"""Hosted-dashboard bridge for MCP OAuth browser callbacks."""

import asyncio
import threading

import pytest


def test_dashboard_flow_exposes_authorization_url_and_accepts_callback():
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="flow-1",
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/mcp/oauth/callback/flow-1",
    )

    asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=s1"))
    assert flow.snapshot() == {
        "flow_id": "flow-1",
        "server_name": "reports",
        "status": "authorization_required",
        "authorization_url": "https://idp.example/authorize?state=s1",
        "error": None,
    }

    flow.deliver_callback(code="code-1", state="s1", error=None)
    assert asyncio.run(flow.wait_for_callback()) == ("code-1", "s1")


def test_dashboard_flow_accepts_only_one_concurrent_callback():
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="flow-race",
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/mcp/oauth/callback/flow-race",
    )
    asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=state"))

    start = threading.Barrier(3)
    outcomes: list[str] = []

    def deliver(code: str) -> None:
        start.wait()
        try:
            flow.deliver_callback(code=code, state="state", error=None)
            outcomes.append("accepted")
        except ValueError:
            outcomes.append("rejected")

    workers = [threading.Thread(target=deliver, args=(code,)) for code in ("one", "two")]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join()

    assert sorted(outcomes) == ["accepted", "rejected"]


def test_mcp_oauth_helpers_use_dashboard_flow_without_loopback_port():
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow, dashboard_oauth_flow
    from tools.mcp_oauth import (
        HermesTokenStorage,
        _build_client_metadata,
        _configure_callback_port,
        _make_callback_waiter,
        _make_redirect_handler,
    )

    flow = DashboardOAuthFlow(
        flow_id="flow-4",
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/mcp/oauth/callback/flow-4",
    )
    cfg = {}
    with dashboard_oauth_flow(flow):
        assert _configure_callback_port(cfg, HermesTokenStorage("reports")) == 0
        metadata = _build_client_metadata(cfg)
        assert str(metadata.redirect_uris[0]) == flow.redirect_uri

        asyncio.run(
            _make_redirect_handler(0)(
                "https://idp.example/authorize?state=state-4"
            )
        )
        flow.deliver_callback(code="code-4", state="state-4", error=None)
        # mcp 2.0's callback_handler contract returns an
        # AuthorizationCodeResult, not the legacy (code, state) tuple.
        result = asyncio.run(_make_callback_waiter(0)())
        assert (result.code, result.state) == ("code-4", "state-4")

    assert flow.authorization_url == "https://idp.example/authorize?state=state-4"


def test_failed_reauth_rollback_preserves_newer_oauth_state(tmp_path, monkeypatch):
    from tools.mcp_oauth import HermesTokenStorage

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("reports")
    storage._tokens_path().parent.mkdir(parents=True)
    storage._tokens_path().write_text("OLD")
    backup = storage.snapshot()
    storage.remove()

    storage._tokens_path().write_text("FRESH")
    storage.restore(backup, only_if_absent=True)

    assert storage._tokens_path().read_text() == "FRESH"


def test_dashboard_flow_propagates_iss_through_callback():
    """RFC 9207 ``iss`` is required by modern MCP servers (Cloudflare,
    Atlassian, Notion, GitHub MCP). The dashboard-relay path used to drop
    it on the floor — verify it now flows through.
    """
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="flow-iss",
        server_name="cloudflare",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/mcp/oauth/callback/cloudflare",
    )

    asyncio.run(flow.publish_authorization_url("https://mcp.cloudflare.com/authorize?state=iss-state"))

    flow.deliver_callback(
        code="code-iss",
        state="iss-state",
        error=None,
        iss="https://mcp.cloudflare.com",
    )

    code, state, iss = asyncio.run(flow.wait_for_callback_full())
    assert (code, state, iss) == ("code-iss", "iss-state", "https://mcp.cloudflare.com")

    # Legacy tuple shape must still work for older callers.
    assert asyncio.run(flow.wait_for_callback()) == ("code-iss", "iss-state")


def test_dashboard_flow_iss_defaults_to_none_when_absent():
    """If the authorization server didn't append ``iss`` (or it got
    dropped by an older dashboard), the SDK still receives ``iss=None`` —
    which is exactly what the legacy behaviour produced, so this is a no-op
    for non-RFC-9207 providers.
    """
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    flow = DashboardOAuthFlow(
        flow_id="flow-no-iss",
        server_name="legacy",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/mcp/oauth/callback/legacy",
    )

    asyncio.run(flow.publish_authorization_url("https://idp.example/authorize?state=legacy-state"))

    flow.deliver_callback(code="code-legacy", state="legacy-state", error=None)

    assert asyncio.run(flow.wait_for_callback_full()) == ("code-legacy", "legacy-state", None)


def test_mcp_oauth_callback_waiter_surfaces_dashboard_iss_to_sdk():
    """End-to-end: dashboard delivers a callback carrying ``iss`` and the
    SDK-shaped result the consumer hands back contains it. This is the exact
    Cloudflare regression — without this, the SDK raises
    ``Authorization response missing iss parameter advertised by the
    authorization server``.
    """
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow, dashboard_oauth_flow
    from tools.mcp_oauth import (
        HermesTokenStorage,
        _build_client_metadata,
        _configure_callback_port,
        _make_callback_waiter,
        _make_redirect_handler,
    )

    flow = DashboardOAuthFlow(
        flow_id="flow-iss-sdk",
        server_name="cloudflare",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/mcp/oauth/callback/cloudflare",
    )
    cfg = {}
    with dashboard_oauth_flow(flow):
        _configure_callback_port(cfg, HermesTokenStorage("cloudflare"))
        _build_client_metadata(cfg)
        asyncio.run(
            _make_redirect_handler(0)(
                "https://mcp.cloudflare.com/authorize?state=cloudflare-state"
            )
        )
        flow.deliver_callback(
            code="code-cloud",
            state="cloudflare-state",
            error=None,
            iss="https://mcp.cloudflare.com",
        )
        result = asyncio.run(_make_callback_waiter(0)())

        # mcp 2.0's AuthorizationCodeResult carries iss as a top-level field
        # that the SDK validator then compares against the discovered
        # metadata. Verify the dashboard-relay path now produces it.
        assert result.code == "code-cloud"
        assert result.state == "cloudflare-state"
        assert result.iss == "https://mcp.cloudflare.com"
