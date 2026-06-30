"""Tests for McpReachLayer and the MCP server.

Covers constructor behavior, lifecycle logs, run_loop no-op,
and _handle_call_tool SSE stream aggregation and finished flag mapping,
error handling (submit_input failures), stream timeout, session-start
guard (fires once per session, resets after session ends), and standard MCP
JSON-RPC protocol endpoints (SSE, tool listing, call tool, auth, namespacing).
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
import mcp.types as types

from reach_layer_base import DoneEvent, SentenceEvent, TextChannelBase
from src.mcp_reach import McpReachLayer
from src.server import _handle_call_tool, create_app, current_caller_agent_id


def _make_config(
    port: int | None = None,
    tool_timeout_s: float | None = None,
    callers: list[dict] | None = None
) -> dict:
    """Helper to generate a base configuration dict."""
    cfg = {
        "agent_core_client": {
            "endpoint": "http://localhost:8000/process_turn",
            "timeout_s": 30.0,
        },
        "reach_layer": {
            "channels": {
                "mcp": {
                    "assembly_mode": "session",
                }
            }
        },
    }
    if port is not None:
        cfg["reach_layer"]["channels"]["mcp"]["port"] = port
    if tool_timeout_s is not None:
        cfg["reach_layer"]["channels"]["mcp"]["tool_timeout_s"] = tool_timeout_s
    if callers is not None:
        cfg["reach_layer"]["channels"]["mcp"]["callers"] = callers
    return cfg


def _make_mock_reach(session_ended: bool = False) -> MagicMock:
    """Helper to create a fully mocked McpReachLayer with a completing stream."""
    mock_reach = MagicMock(spec=McpReachLayer)
    mock_reach.on_session_start = AsyncMock()
    mock_reach.on_session_end = AsyncMock()
    mock_reach.submit_input = AsyncMock()

    async def _ok_subscribe(session_id: str, user_id: str | None = None):
        yield DoneEvent(turn_status="completed", session_ended=session_ended)

    mock_reach.subscribe_events = _ok_subscribe
    return mock_reach


# ---------------------------------------------------------------------------
# McpReachLayer unit tests
# ---------------------------------------------------------------------------

class TestMcpReachLayer:
    """Unit tests for McpReachLayer class."""

    def test_init_reads_port_from_config(self) -> None:
        """Verify McpReachLayer reads port from reach_layer.channels.mcp.port."""
        layer = McpReachLayer(_make_config(port=8089))
        assert layer._port == 8089

    def test_init_defaults_port(self) -> None:
        """Verify missing mcp config key defaults port to 8007."""
        cfg = {
            "agent_core_client": {
                "endpoint": "http://localhost:8000/process_turn",
                "timeout_s": 30.0,
            },
            "reach_layer": {
                "channels": {}
            }
        }
        layer = McpReachLayer(cfg)
        assert layer._port == 8007

    def test_init_raises_on_none_config(self) -> None:
        """Verify config=None raises ValueError from base class."""
        with pytest.raises(ValueError, match="config must not be None"):
            McpReachLayer(None)

    def test_init_calls_super_with_mcp_channel_name(self) -> None:
        """Verify self.channel_name == "mcp"."""
        layer = McpReachLayer(_make_config())
        assert layer.channel_name == "mcp"

    def test_init_assembly_mode_is_session(self) -> None:
        """Verify self.assembly_mode == "session"."""
        layer = McpReachLayer(_make_config())
        assert layer.assembly_mode == "session"

    @pytest.mark.asyncio
    async def test_on_session_start_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify on_session_start does not raise and emits structured log."""
        layer = McpReachLayer(_make_config())
        with caplog.at_level(logging.INFO):
            await layer.on_session_start("session-123", "user-456")

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.message == "mcp_reach.session_start"
        assert record.session_id == "session-123"
        assert record.user_id == "user-456"

    @pytest.mark.asyncio
    async def test_on_session_start_invalid(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify on_session_start with empty session_id logs a warning."""
        layer = McpReachLayer(_make_config())
        with caplog.at_level(logging.WARNING):
            await layer.on_session_start("", "user-456")
        assert "mcp_reach.session_start_invalid" in caplog.text

    @pytest.mark.asyncio
    async def test_on_session_end_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify on_session_end does not raise and emits structured log."""
        layer = McpReachLayer(_make_config())
        with caplog.at_level(logging.INFO):
            await layer.on_session_end("session-123")

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.message == "mcp_reach.session_end"
        assert record.session_id == "session-123"

    @pytest.mark.asyncio
    async def test_on_session_end_invalid(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify on_session_end with empty session_id logs a warning."""
        layer = McpReachLayer(_make_config())
        with caplog.at_level(logging.WARNING):
            await layer.on_session_end(" ")
        assert "mcp_reach.session_end_invalid" in caplog.text

    @pytest.mark.asyncio
    async def test_run_loop_is_noop(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify run_loop() returns without raising and logs skipped status."""
        layer = McpReachLayer(_make_config())
        with caplog.at_level(logging.INFO):
            await layer.run_loop()
        assert "mcp_reach.run_loop_noop" in caplog.text

    def test_abc_contract_enforced(self) -> None:
        """Verify instantiating incomplete subclass (missing abstract methods) raises TypeError."""
        class IncompleteChannel(TextChannelBase):
            pass
        with pytest.raises(TypeError):
            IncompleteChannel(_make_config(), "incomplete")


# ---------------------------------------------------------------------------
# server._handle_call_tool — normal / edge path tests
# ---------------------------------------------------------------------------

class TestHandleCallTool:
    """Unit tests for _handle_call_tool helper function."""

    @pytest.mark.asyncio
    async def test_handle_call_tool_aggregates_sentences(self) -> None:
        """Verify multiple SentenceEvent texts are joined and returned in reply."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.on_session_end = AsyncMock()
        mock_reach.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            yield SentenceEvent(text="Hello", sentence_index=0, turn_id="t1")
            yield SentenceEvent(text="world.", sentence_index=1, turn_id="t1")
            yield DoneEvent(turn_status="completed", session_ended=False)

        mock_reach.subscribe_events = fake_subscribe

        result = await _handle_call_tool(mock_reach, "s-1", "hi")
        assert result["reply"] == "Hello world."
        assert result["session_id"] == "s-1"
        assert result["finished"] is False
        assert result["error_type"] is None
        assert result["error_message"] is None
        mock_reach.on_session_start.assert_awaited_once_with("s-1", "")
        mock_reach.on_session_end.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_call_tool_finished_false_when_session_not_ended(self) -> None:
        """Verify finished=False when DoneEvent.session_ended=False."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.on_session_end = AsyncMock()
        mock_reach.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            yield DoneEvent(turn_status="completed", session_ended=False)

        mock_reach.subscribe_events = fake_subscribe

        result = await _handle_call_tool(mock_reach, "s-1", "hi")
        assert result["finished"] is False
        mock_reach.on_session_end.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_call_tool_finished_true_when_session_ended(self) -> None:
        """Verify finished=True when DoneEvent.session_ended=True and on_session_end called."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.on_session_end = AsyncMock()
        mock_reach.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            yield SentenceEvent(text="Bye.", sentence_index=0, turn_id="t2")
            yield DoneEvent(turn_status="completed", session_ended=True)

        mock_reach.subscribe_events = fake_subscribe

        result = await _handle_call_tool(mock_reach, "s-1", "exit")
        assert result["finished"] is True
        assert result["reply"] == "Bye."
        mock_reach.on_session_end.assert_awaited_once_with("s-1")

    @pytest.mark.asyncio
    async def test_handle_call_tool_DoneEvent_with_error_propagates(self) -> None:
        """Verify that a DoneEvent carrying error fields propagates them."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.on_session_end = AsyncMock()
        mock_reach.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            yield DoneEvent(
                turn_status="abandoned",
                session_ended=True,
                error_type="provider_failure",
                error_message="The model failed to generate a response."
            )

        mock_reach.subscribe_events = fake_subscribe

        result = await _handle_call_tool(mock_reach, "s-1", "test")
        assert result["finished"] is True
        assert result["error_type"] == "provider_failure"
        assert result["error_message"] == "The model failed to generate a response."
        mock_reach.on_session_end.assert_awaited_once_with("s-1")

    @pytest.mark.asyncio
    async def test_handle_call_tool_empty_reply_on_no_sentences(self) -> None:
        """Verify DoneEvent with no preceding SentenceEvent returns reply=""."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.on_session_end = AsyncMock()
        mock_reach.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            yield DoneEvent(turn_status="completed", session_ended=False)

        mock_reach.subscribe_events = fake_subscribe

        result = await _handle_call_tool(mock_reach, "s-1", "nothing")
        assert result["reply"] == ""
        assert result["finished"] is False

    @pytest.mark.asyncio
    async def test_handle_call_tool_does_not_call_parse_sse_event_directly(self) -> None:
        """Verify ReachLayerBase._parse_sse_event is never called by _handle_call_tool."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.on_session_end = AsyncMock()
        mock_reach.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            yield DoneEvent(turn_status="completed", session_ended=False)

        mock_reach.subscribe_events = fake_subscribe

        with patch("reach_layer_base.ReachLayerBase._parse_sse_event") as mock_parse:
            await _handle_call_tool(mock_reach, "s-1", "msg")
            mock_parse.assert_not_called()

    @pytest.mark.asyncio
    async def test_fire_session_start_false_skips_hook(self) -> None:
        """Verify fire_session_start=False bypasses on_session_start."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.on_session_end = AsyncMock()
        mock_reach.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            yield DoneEvent(turn_status="completed", session_ended=False)

        mock_reach.subscribe_events = fake_subscribe

        await _handle_call_tool(mock_reach, "s-1", "hi", fire_session_start=False)
        mock_reach.on_session_start.assert_not_awaited()


# ---------------------------------------------------------------------------
# server._handle_call_tool — failure path tests
# ---------------------------------------------------------------------------

class TestHandleCallToolFailures:
    """Failure-path tests for _handle_call_tool: submit_input errors and stream timeout."""

    @pytest.mark.asyncio
    async def test_submit_input_timeout_returns_error_response(self) -> None:
        """submit_input raising TimeoutException → error_type='timeout', finished=True."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.submit_input = AsyncMock(
            side_effect=httpx.TimeoutException("connect timed out")
        )

        result = await _handle_call_tool(mock_reach, "s-err", "hi")

        assert result["reply"] == ""
        assert result["session_id"] == "s-err"
        assert result["finished"] is True
        assert result["error_type"] == "timeout"
        assert result["error_message"] == "Agent Core did not respond in time."

    @pytest.mark.asyncio
    async def test_submit_input_http_error_returns_error_response(self) -> None:
        """submit_input raising HTTPStatusError → error_type='upstream_error', finished=True."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 503
        http_error = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=mock_response
        )
        mock_reach.submit_input = AsyncMock(side_effect=http_error)

        result = await _handle_call_tool(mock_reach, "s-err", "hi")

        assert result["reply"] == ""
        assert result["finished"] is True
        assert result["error_type"] == "upstream_error"
        assert "503" in result["error_message"]

    @pytest.mark.asyncio
    async def test_submit_input_unexpected_error_returns_error_response(self) -> None:
        """submit_input raising unexpected RuntimeError → error_type='internal_error'."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.submit_input = AsyncMock(side_effect=RuntimeError("boom"))

        result = await _handle_call_tool(mock_reach, "s-err", "hi")

        assert result["reply"] == ""
        assert result["finished"] is True
        assert result["error_type"] == "internal_error"
        assert result["error_message"] == "Unexpected error submitting to Agent Core."

    @pytest.mark.asyncio
    async def test_subscribe_events_timeout_returns_partial_reply(self) -> None:
        """subscribe_events stalling past timeout → error_type='stream_timeout', partial reply."""
        mock_reach = MagicMock(spec=McpReachLayer)
        mock_reach.on_session_start = AsyncMock()
        mock_reach.submit_input = AsyncMock()

        async def stalling_subscribe(session_id: str, user_id: str | None = None):
            yield SentenceEvent(text="Hello", sentence_index=0, turn_id="t1")
            await asyncio.sleep(10)  # stall — will be cancelled by wait_for
            yield DoneEvent(turn_status="completed", session_ended=False)

        mock_reach.subscribe_events = stalling_subscribe

        result = await _handle_call_tool(
            mock_reach, "s-stall", "hi", tool_timeout_s=0.05
        )

        assert result["error_type"] == "stream_timeout"
        assert result["reply"] == "Hello"    # partial sentences already received
        assert result["finished"] is True

    @pytest.mark.asyncio
    async def test_on_session_start_fires_only_on_first_call(self) -> None:
        """create_app active-session guard: on_session_start fires exactly once per session."""
        layer = McpReachLayer(_make_config())
        layer.on_session_start = AsyncMock()
        layer.on_session_end = AsyncMock()
        layer.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            yield DoneEvent(turn_status="completed", session_ended=False)

        layer.subscribe_events = fake_subscribe

        callers = [{"caller_agent_id": "agent-1", "api_key": "secret"}]
        app = create_app(layer, _make_config(callers=callers))

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            auth = {"Authorization": "Bearer secret"}
            await client.post("/call_tool", json={"session_id": "multi-1", "text": "turn 1"}, headers=auth)
            await client.post("/call_tool", json={"session_id": "multi-1", "text": "turn 2"}, headers=auth)

        # on_session_start fires exactly once — second turn reuses the active session.
        layer.on_session_start.assert_awaited_once_with("agent-1:multi-1", "")

    @pytest.mark.asyncio
    async def test_on_session_start_fires_again_after_session_ends(self) -> None:
        """After session_ended=True, a new call with same session_id re-fires on_session_start."""
        layer = McpReachLayer(_make_config())
        layer.on_session_start = AsyncMock()
        layer.on_session_end = AsyncMock()
        layer.submit_input = AsyncMock()

        call_count = 0

        async def fake_subscribe(session_id: str, user_id: str | None = None):
            nonlocal call_count
            call_count += 1
            yield DoneEvent(
                turn_status="completed",
                session_ended=(call_count == 1),
            )

        layer.subscribe_events = fake_subscribe

        callers = [{"caller_agent_id": "agent-1", "api_key": "secret"}]
        app = create_app(layer, _make_config(callers=callers))

        from httpx import AsyncClient, ASGITransport
        auth = {"Authorization": "Bearer secret"}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp1 = await client.post(
                "/call_tool", json={"session_id": "reuse-1", "text": "first"}, headers=auth
            )
            assert resp1.json()["finished"] is True

            await client.post(
                "/call_tool", json={"session_id": "reuse-1", "text": "reconnect"}, headers=auth
            )

        assert layer.on_session_start.await_count == 2


# ---------------------------------------------------------------------------
# MCP Protocol Tests (Auth, Namespacing, JSON-RPC endpoints)
# ---------------------------------------------------------------------------

class TestMcpServerEndpoints:
    """Tests verification of MCP JSON-RPC over SSE endpoints, auth, and namespacing."""

    def test_health_endpoint(self) -> None:
        """Verify health check returns status ok."""
        layer = McpReachLayer(_make_config())
        app = create_app(layer, _make_config())
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_call_tool_auth_required_if_callers_present(self) -> None:
        """Verify REST call_tool endpoint returns 401 if callers configured but auth missing."""
        layer = McpReachLayer(_make_config())
        callers = [{"caller_agent_id": "agent-1", "api_key": "secret"}]
        app = create_app(layer, _make_config(callers=callers))
        client = TestClient(app)

        resp = client.post("/call_tool", json={"session_id": "s1", "text": "hi"})
        assert resp.status_code == 401

        # Providing invalid key
        resp = client.post(
            "/call_tool",
            json={"session_id": "s1", "text": "hi"},
            headers={"Authorization": "Bearer badkey"}
        )
        assert resp.status_code == 401

        # Providing valid key
        layer.submit_input = AsyncMock()
        async def fake_subscribe(session_id: str):
            yield DoneEvent(turn_status="completed", session_ended=True)
        layer.subscribe_events = fake_subscribe

        resp = client.post(
            "/call_tool",
            json={"session_id": "s1", "text": "hi"},
            headers={"Authorization": "Bearer secret"}
        )
        assert resp.status_code == 200
        # Verify submit_input is called with namespaced session_id
        layer.submit_input.assert_called_once_with(
            "agent-1:s1", "hi", user_id=None, caller_agent_id="agent-1", locale=None, metadata=None
        )

    @pytest.mark.asyncio
    async def test_mcp_call_tool_handler_success(self) -> None:
        """Verify handle_call_tool correctly aggregates and namespaces session_id."""
        layer = McpReachLayer(_make_config())
        layer.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str):
            yield SentenceEvent(text="Hello from DPG.", sentence_index=0)
            yield DoneEvent(turn_status="completed", session_ended=False)
        layer.subscribe_events = fake_subscribe

        callers = [{"caller_agent_id": "callerA", "api_key": "keyA"}]
        app = create_app(layer, _make_config(callers=callers))

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Authorization: Bearer header auth (query-param auth is not supported)
            resp = await client.post(
                "/call_tool",
                json={"session_id": "123", "text": "test turn"},
                headers={"Authorization": "Bearer keyA"}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["reply"] == "Hello from DPG."
            assert data["finished"] is False
            assert data["session_id"] == "123"

            layer.submit_input.assert_called_with(
                "callerA:123", "test turn", user_id=None, caller_agent_id="callerA", locale=None, metadata=None
            )

    @pytest.mark.asyncio
    async def test_mcp_progress_callback(self) -> None:
        """Verify progress notifications are triggered during subscription."""
        layer = McpReachLayer(_make_config())
        layer.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str):
            yield SentenceEvent(text="Sentence 1", sentence_index=0)
            yield SentenceEvent(text="Sentence 2", sentence_index=1)
            yield DoneEvent(turn_status="completed", session_ended=False)
        layer.subscribe_events = fake_subscribe

        app = create_app(layer, _make_config())

        # Mock RequestContext & session to capture progress notification
        mock_ctx = MagicMock()
        mock_ctx.meta = {"progressToken": "token-xyz"}
        mock_session = AsyncMock()
        mock_ctx.session = mock_session

        handle_call_tool = app.state.handle_call_tool
        result = await handle_call_tool("dpg.send_message", {"session_id": "abc", "message": "hello"}, mock_ctx)
        
        # Verify result format
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert payload["reply"] == "Sentence 1 Sentence 2"

        # Verify progress calls
        assert mock_session.send_notification.call_count == 2
        mock_session.send_notification.assert_any_call(
            types.ProgressNotification(
                method="notifications/progress",
                params=types.ProgressNotificationParams(
                    progressToken="token-xyz",
                    progress=0.0,
                    meta=types.NotificationParams.Meta(
                        extra={
                            "text": "Sentence 1",
                            "sentence_index": 0,
                        }
                    )
                )
            )
        )
        mock_session.send_notification.assert_any_call(
            types.ProgressNotification(
                method="notifications/progress",
                params=types.ProgressNotificationParams(
                    progressToken="token-xyz",
                    progress=1.0,
                    meta=types.NotificationParams.Meta(
                        extra={
                            "text": "Sentence 2",
                            "sentence_index": 1,
                        }
                    )
                )
            )
        )

    @pytest.mark.asyncio
    async def test_mcp_call_tool_with_locale_and_metadata(self) -> None:
        """Verify locale and metadata are propagated down through REST /call_tool."""
        layer = McpReachLayer(_make_config())
        layer.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str):
            yield DoneEvent(turn_status="completed", session_ended=True)
        layer.subscribe_events = fake_subscribe

        callers = [{"caller_agent_id": "callerA", "api_key": "keyA"}]
        app = create_app(layer, _make_config(callers=callers))

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/call_tool",
                json={
                    "session_id": "123",
                    "text": "test turn",
                    "locale": "en_US",
                    "metadata": {"consent_granted": True}
                },
                headers={"Authorization": "Bearer keyA"}
            )
            assert resp.status_code == 200
            layer.submit_input.assert_called_once_with(
                "callerA:123", "test turn", user_id=None, caller_agent_id="callerA",
                locale="en_US", metadata={"consent_granted": True}
            )


# ---------------------------------------------------------------------------
# Security regression tests (GH-338 re-review: pullrequestreview-4590269104)
# ---------------------------------------------------------------------------

class TestSecurityRegressions:
    """Explicit security tests covering all re-review blocker and non-blocking items."""

    def test_call_tool_returns_503_when_no_callers_configured(self) -> None:
        """Blocker 1: /call_tool returns 503 when callers list is empty (fail-closed)."""
        layer = McpReachLayer(_make_config())
        app = create_app(layer, _make_config())
        client = TestClient(app)

        resp = client.post("/call_tool", json={"session_id": "s1", "text": "hi"})
        assert resp.status_code == 503
        assert "callers" in resp.json()["detail"]

    def test_sse_returns_503_when_no_callers_configured(self) -> None:
        """Blocker 1: /sse returns 503 when callers list is empty (fail-closed)."""
        layer = McpReachLayer(_make_config())
        app = create_app(layer, _make_config())
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/sse", headers={"Accept": "text/event-stream"})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_handle_call_tool_ignores_client_supplied_caller_agent_id(self) -> None:
        """Blocker 2: server ignores caller_agent_id in JSON arguments; uses auth-derived ID.

        An attacker sending caller_agent_id='attacker' in arguments must not be able
        to impersonate another caller or bypass the consent gate.
        """
        layer = McpReachLayer(_make_config())
        layer.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str):
            yield DoneEvent(turn_status="completed", session_ended=False)
        layer.subscribe_events = fake_subscribe

        callers = [{"caller_agent_id": "legit-agent", "api_key": "good-key"}]
        app = create_app(layer, _make_config(callers=callers))

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/call_tool",
                json={
                    "session_id": "s1",
                    "text": "hi",
                    "caller_agent_id": "attacker"  # client-supplied, must be ignored
                },
                headers={"Authorization": "Bearer good-key"}
            )
            assert resp.status_code == 200

        # Verify submit_input was called with the auth-derived ID, not the client-supplied one.
        layer.submit_input.assert_called_once_with(
            "legit-agent:s1", "hi", user_id=None,
            caller_agent_id="legit-agent",  # must be auth-derived, never "attacker"
            locale=None, metadata=None
        )

    def test_hmac_timing_safe_comparison_rejects_similar_key(self) -> None:
        """NB-1: Keys differing by one char must be rejected (timing-safe comparison)."""
        from fastapi import Request as FastAPIRequest
        from unittest.mock import MagicMock

        callers = [{"caller_agent_id": "agent-1", "api_key": "correct-key-abc"}]

        mock_request = MagicMock(spec=FastAPIRequest)
        mock_request.headers = {"Authorization": "Bearer correct-key-abX"}  # 1 char different
        mock_request.query_params = {}

        from src.server import _authenticate_request
        from fastapi import HTTPException as FastHTTPException
        with pytest.raises(FastHTTPException) as exc_info:
            _authenticate_request(mock_request, callers)
        assert exc_info.value.status_code == 401

    def test_query_param_api_key_is_rejected(self) -> None:
        """NB-2: ?api_key= query-param auth must be rejected (leaks keys into logs)."""
        layer = McpReachLayer(_make_config())
        callers = [{"caller_agent_id": "agent-1", "api_key": "secret"}]
        app = create_app(layer, _make_config(callers=callers))
        client = TestClient(app)

        # Even with the correct key in the query param, it must not authenticate.
        resp = client.post(
            "/call_tool",
            json={"session_id": "s1", "text": "hi"},
            params={"api_key": "secret"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_sse_integration_with_real_client(self) -> None:
        """Verify standard MCP SSE tool calls function end-to-end with a real client session.

        Uses a background uvicorn server thread to prevent in-process ASGI deadlocks.
        """
        import threading
        import time
        import uvicorn
        from mcp.client.sse import sse_client
        from mcp.client.session import ClientSession

        layer = McpReachLayer(_make_config())
        layer.submit_input = AsyncMock()

        async def fake_subscribe(session_id: str):
            yield SentenceEvent(text="Integrated SSE sentence.", sentence_index=0)
            yield DoneEvent(turn_status="completed", session_ended=True)
        layer.subscribe_events = fake_subscribe

        callers = [{"caller_agent_id": "callerA", "api_key": "keyA"}]
        app = create_app(layer, _make_config(callers=callers))

        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        def run_server():
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

        server_thread = threading.Thread(target=run_server, daemon=True)
        server_thread.start()
        time.sleep(0.5)

        headers = {"Authorization": "Bearer keyA"}
        url = f"http://127.0.0.1:{port}/sse"
        
        async with sse_client(url, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                tools_result = await session.list_tools()
                assert len(tools_result.tools) == 1
                assert tools_result.tools[0].name == "dpg.send_message"

                result = await session.call_tool("dpg.send_message", {"session_id": "s-integrated", "message": "hello"})
                assert len(result.content) == 1
                assert result.content[0].type == "text"
                
                payload = json.loads(result.content[0].text)
                assert payload["reply"] == "Integrated SSE sentence."
                assert payload["session_id"] == "s-integrated"
                assert payload["finished"] is True
