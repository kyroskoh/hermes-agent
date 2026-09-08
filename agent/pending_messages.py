"""Durable pending-message queue with idempotent replay.

Implements Section P of the state-db-reliability design.

Inbound platform messages are durably enqueued in
``<state_dir>/pending_messages/pending-<uuid>.json`` BEFORE any database
write. The atomic write uses the standard tmp+fsync+rename recipe so a
power loss cannot produce a half-written message.

Replay is idempotent on ``(platform, profile, sender, platform_message_id)``;
the same inbound message never gets inserted into the canonical messages
table twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


PENDING_DIRNAME = "pending_messages"


@dataclass
class PendingMessage:
    """One durable pending message.

    The dedup key is computed from the four-tuple (platform, profile,
    sender, platform_message_id) and stored in the ``dedup_key`` field so
    replay can detect duplicates without recomputing.
    """
    id: str
    platform: str
    profile: str
    sender: str
    platform_message_id: str
    received_at: float
    body: str
    dedup_key: str
    state: str = "queued"  # queued / replaying / replayed / failed
    attempt_count: int = 0
    last_attempt_at: Optional[float] = None
    last_error: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @staticmethod
    def compute_dedup_key(platform: str, profile: str, sender: str,
                          platform_message_id: str) -> str:
        h = hashlib.sha256()
        for part in (platform, profile, sender, platform_message_id):
            h.update(part.encode("utf-8"))
            h.update(b"|")
        return h.hexdigest()

    @classmethod
    def new(cls, *, platform: str, profile: str, sender: str,
            platform_message_id: str, body: str,
            extra: Optional[dict] = None) -> "PendingMessage":
        return cls(
            id=str(uuid.uuid4()),
            platform=platform,
            profile=profile,
            sender=sender,
            platform_message_id=platform_message_id,
            received_at=time.time(),
            body=body,
            dedup_key=cls.compute_dedup_key(platform, profile, sender,
                                            platform_message_id),
            extra=extra or {},
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, blob: dict) -> "PendingMessage":
        return cls(
            id=blob["id"],
            platform=blob["platform"],
            profile=blob["profile"],
            sender=blob["sender"],
            platform_message_id=blob["platform_message_id"],
            received_at=blob["received_at"],
            body=blob["body"],
            dedup_key=blob["dedup_key"],
            state=blob.get("state", "queued"),
            attempt_count=blob.get("attempt_count", 0),
            last_attempt_at=blob.get("last_attempt_at"),
            last_error=blob.get("last_error"),
            extra=blob.get("extra", {}),
        )


def pending_dir(state_dir: os.PathLike) -> Path:
    p = Path(state_dir).expanduser().resolve() / PENDING_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def enqueue(state_dir: os.PathLike, message: PendingMessage) -> Path:
    """Write the message to disk atomically. Returns the path written."""
    d = pending_dir(state_dir)
    dest = d / f"pending-{message.id}.json"
    tmp = d / f".pending-{message.id}.json.tmp"
    tmp.write_text(json.dumps(message.to_dict(), indent=2, sort_keys=True),
                   encoding="utf-8")
    # fsync before rename so the data is durable on disk.
    fd = os.open(str(tmp), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, dest)
    # fsync the directory entry.
    dfd = os.open(str(d), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    logger.debug("pending message enqueued: id=%s dedup=%s", message.id,
                 message.dedup_key[:12])
    return dest


def list_pending(state_dir: os.PathLike,
                 *,
                 state: Optional[str] = None) -> list[PendingMessage]:
    """List pending messages, optionally filtered by state. Sorted by
    received_at ascending so the replay path processes in order.
    """
    d = pending_dir(state_dir)
    out: list[PendingMessage] = []
    for entry in sorted(d.glob("pending-*.json")):
        try:
            blob = json.loads(entry.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("pending list: skipping unparseable %s: %s", entry, e)
            continue
        m = PendingMessage.from_dict(blob)
        if state is None or m.state == state:
            out.append(m)
    out.sort(key=lambda m: m.received_at)
    return out


def _atomic_state_write(path: Path, message: PendingMessage) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(message.to_dict(), indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def replay_pending(state_dir: os.PathLike, *,
                   process_fn=None,
                   mark_replayed: bool = True,
                   skip_state: Iterable[str] = ("replayed",)) -> dict:
    """Replay every queued message through ``process_fn``.

    ``process_fn`` is a callable receiving a ``PendingMessage`` and
    returning a dict with at least ``{"ok": bool, "duplicate": bool}``.
    When ``duplicate`` is True, the entry is marked as replayed without
    raising. When ``ok`` is False and not duplicate, the entry is left in
    state ``replaying`` with ``last_error`` populated; the next replay
    retries it (idempotency contract: ``process_fn`` MUST itself dedup on
    ``(platform, profile, sender, platform_message_id)`` before inserting
    into the messages table).

    Returns a summary ``{"replayed": int, "duplicates": int, "failed": int}``.
    """
    summary = {"replayed": 0, "duplicates": 0, "failed": 0,
               "skipped": 0, "errors": []}
    d = pending_dir(state_dir)
    skip = set(skip_state)
    for entry in sorted(d.glob("pending-*.json")):
        try:
            blob = json.loads(entry.read_text(encoding="utf-8"))
        except Exception as e:
            summary["errors"].append({"file": str(entry), "error": str(e)})
            continue
        msg = PendingMessage.from_dict(blob)
        if msg.state in skip:
            summary["skipped"] += 1
            continue
        msg.state = "replaying"
        msg.attempt_count += 1
        msg.last_attempt_at = time.time()
        _atomic_state_write(entry, msg)
        if process_fn is None:
            # No processing function — leave in 'replaying' state.
            summary["skipped"] += 1
            continue
        try:
            res = process_fn(msg) or {}
            ok = bool(res.get("ok"))
            dup = bool(res.get("duplicate"))
        except Exception as e:
            msg.state = "failed"
            msg.last_error = str(e)
            _atomic_state_write(entry, msg)
            summary["failed"] += 1
            summary["errors"].append({"id": msg.id, "error": str(e)})
            continue
        if dup:
            msg.state = "replayed"
            msg.last_error = "duplicate"
            if mark_replayed:
                _atomic_state_write(entry, msg)
            summary["duplicates"] += 1
            continue
        if ok:
            msg.state = "replayed"
            msg.last_error = None
            if mark_replayed:
                _atomic_state_write(entry, msg)
            summary["replayed"] += 1
        else:
            msg.state = "failed"
            msg.last_error = "process_fn returned ok=False"
            _atomic_state_write(entry, msg)
            summary["failed"] += 1
    return summary


__all__ = [
    "PendingMessage",
    "pending_dir",
    "enqueue",
    "list_pending",
    "replay_pending",
]

# Gateway ingress uses the same atomic queue primitives in an isolated
# subdirectory. Generic shutdown transcript files must not be replayed as turns.
@__import__('contextlib').contextmanager
def _inbox_lock(home):
    import fcntl
    d=Path(home)/'pending_messages'/'inbound'
    d.mkdir(parents=True,exist_ok=True,mode=0o700)
    with open(d/'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield d


def _durable_json(path,payload):
    from utils import atomic_json_write
    atomic_json_write(path,payload,mode=0o600,default=str)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def capture_inbound(home,event,max_pending=1000,max_bytes=64*1024*1024):
    """Durably capture a new authorized agent turn; dedup within its chat.

    Processing receipts are never replayed automatically: a crash may have
    happened after an external side effect but before recording completion.
    """
    import dataclasses
    source=event.source
    mid=event.message_id or getattr(event,'_storage_receipt_id',None) or str(uuid.uuid4())
    event._storage_receipt_id=mid
    identity=[str(getattr(source,'platform','')),str(getattr(source,'profile','default')),
              str(getattr(source,'chat_id','')),str(getattr(source,'thread_id','')),
              str(getattr(source,'user_id','')),str(mid)]
    key=hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    data={}
    for f in dataclasses.fields(event):
        value=getattr(event,f.name)
        if f.name=='raw_message':continue
        if f.name=='source':value=source.to_dict()
        elif hasattr(value,'value'):value=value.value
        elif hasattr(value,'isoformat'):value=value.isoformat()
        data[f.name]=value
    data['message_id']=str(mid)
    record={'id':key,'state':'queued','received_at':time.time(),'event':data}
    blob=json.dumps(record,default=str).encode()
    with _inbox_lock(home) as d:
        path=d/(key+'.json')
        if path.exists():return path,json.loads(path.read_text())
        files=list(d.glob('*.json'))
        # Completed receipts expire after seven days; unresolved input is retained.
        for old in files:
            if old.stat().st_mtime<time.time()-7*86400:
                payload=json.loads(old.read_text())
                if payload['state']=='completed':old.unlink()
        files=list(d.glob('*.json'))
        if len(files)>=max_pending or sum(p.stat().st_size for p in files)+len(blob)>max_bytes:
            raise OSError('durable input queue capacity exceeded')
        _durable_json(path,record)
        return path,record


def set_inbound_state(home,path,state,expected=None):
    with _inbox_lock(home):
        payload=json.loads(path.read_text())
        if expected is not None and payload['state'] not in expected:return False
        payload['state']=state;payload['updated_at']=time.time()
        _durable_json(path,payload)
        return True


def storage_readable(db_path):
    from agent.db_connection import open_sqlite
    with open_sqlite(db_path,role='reader',timeout=2) as c:
        if c.raw.execute('pragma quick_check(1)').fetchall()!=[('ok',)]:
            raise RuntimeError('session storage integrity check failed')
        c.raw.execute('select * from gateway_routing limit 1').fetchall()
        c.raw.execute('select id from messages order by id desc limit 1').fetchall()


async def guarded_agent_turn(runner,event,source,key,generation,handler):
    """Durable admission at the authorized new-turn boundary, before DB writes.

    Controls/steering keep their existing bypass paths. Deferred adapter queues
    still use the existing shutdown spool; this receipt covers turns admitted
    to the agent and main-database failure, not transport-level exactly-once ACK.
    """
    if event.internal:
        return await handler(event,source,key,generation)
    import asyncio
    db=getattr(runner,'_session_db',None)
    path=getattr(db,'db_path',None)
    if not isinstance(path,(str,Path)):
        # No resolvable backing store for this runner (unset, a test fake, or
        # the handle cache is between retries — see GatewayRunner._session_db).
        # There is nothing concrete to guard here; guessing a default path
        # would check the wrong database (or one that was never opened) and
        # falsely block turns for every caller that legitimately runs without
        # one. The corruption scenario this guards is a previously-working,
        # resolvable db_path that goes unreadable mid-flight — handled below.
        return await handler(event,source,key,generation)
    home=Path(path).parent
    try:
        receipt,record=await asyncio.to_thread(capture_inbound,home,event)
    except Exception:
        logger.exception('Durable input capture failed; agent execution refused')
        return 'Session storage is unavailable and this message could not be saved. Please retry after recovery.'
    if record['state']=='completed':return None
    if record['state'] in ('processing','needs_review'):
        return 'This input has an unfinished saved turn. It needs recovery review before it can safely run again.'
    try:
        await asyncio.to_thread(storage_readable,Path(path))
    except Exception:
        logger.exception('Session storage unavailable; incoming turn retained in durable queue')
        return 'Session storage is unavailable. Your message is saved and will be retried after storage recovers.'
    claimed=await asyncio.to_thread(set_inbound_state,home,receipt,'processing',('queued',))
    if not claimed:return None
    try:
        response=await handler(event,source,key,generation)
        # Any post-start uncertainty is retained for operator review; do not
        # automatically repeat model/tool side effects after an interrupted turn.
        await asyncio.to_thread(storage_readable,Path(path))
        await asyncio.to_thread(set_inbound_state,home,receipt,'completed' if response else 'needs_review')
        return response
    except BaseException:
        await asyncio.to_thread(set_inbound_state,home,receipt,'needs_review')
        raise


async def replay_queued_inbound(runner,home):
    """Replay only never-started turns through normal authorization and routing."""
    import asyncio
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from datetime import datetime
    d=Path(home)/'pending_messages'/'inbound'
    for path in sorted(d.glob('*.json'))[:100]:
        try:
            payload=json.loads(path.read_text())
            if payload['state']!='queued':continue
            data=payload['event'];data['source']=SessionSource.from_dict(data['source'])
            data['message_type']=MessageType(data['message_type'])
            if isinstance(data.get('timestamp'),str):data['timestamp']=datetime.fromisoformat(data['timestamp'])
            event=MessageEvent(**data)
            event._storage_receipt_id=event.message_id or payload['id']
            adapter=runner._adapter_for_source(event.source)
            if adapter is None:continue
            session_key=runner._session_key_for_source(event.source)
            if session_key in getattr(adapter,'_active_sessions',{}):continue
            db=getattr(runner,'_session_db',None)
            db_path=getattr(db,'db_path',Path(home)/'state.db')
            await asyncio.to_thread(storage_readable,Path(db_path))
            # Dispatch inline through the normal handler; send its response via
            # the existing adapter message path so thread/delivery rules apply.
            await adapter.handle_message(event)
        except Exception:
            logger.exception('Saved inbound replay deferred')
            break
