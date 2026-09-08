import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
from agent.pending_messages import capture_inbound,set_inbound_state,guarded_agent_turn
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.config import Platform


def event(mid='1',chat='chat'):
    return MessageEvent(text='hi',message_id=mid,source=SessionSource(platform=Platform.TELEGRAM,chat_id=chat,user_id='u'))


def test_dedup_is_chat_scoped_and_state_transition_is_exclusive(tmp_path):
    p,r=capture_inbound(tmp_path,event())
    assert capture_inbound(tmp_path,event())[0]==p
    assert capture_inbound(tmp_path,event(chat='other'))[0]!=p
    assert set_inbound_state(tmp_path,p,'processing',('queued',))
    assert not set_inbound_state(tmp_path,p,'processing',('queued',))


def test_capacity_refuses_without_discarding(tmp_path):
    p,_=capture_inbound(tmp_path,event(),max_pending=1)
    with pytest.raises(OSError):capture_inbound(tmp_path,event('2'),max_pending=1)
    assert p.exists()


def test_failed_storage_preserves_and_never_calls_agent(tmp_path):
    runner=SimpleNamespace(_session_db=SimpleNamespace(db_path=tmp_path/'missing.db'))
    async def agent(*args):raise AssertionError('must not execute')
    response=asyncio.run(guarded_agent_turn(runner,event(),event().source,'key',1,agent))
    assert 'saved' in response
    import json
    files=list((tmp_path/'pending_messages/inbound').glob('*.json'))
    assert len(files)==1
    assert json.loads(files[0].read_text())['state']=='queued'


def test_capture_failure_prevents_execution(tmp_path,monkeypatch):
    import agent.pending_messages as pm
    def fail(*args,**kwargs):raise OSError('disk full')
    monkeypatch.setattr(pm,'capture_inbound',fail)
    runner=SimpleNamespace(_session_db=SimpleNamespace(db_path=tmp_path/'state.db'))
    async def agent(*args):raise AssertionError('must not execute')
    assert 'could not be saved' in asyncio.run(pm.guarded_agent_turn(runner,event(),event().source,'key',1,agent))
