"""Tests for McpReachLayer and the MCP server.

Covers constructor behavior, lifecycle logs, run_loop no-op,
and _handle_call_tool SSE stream aggregation and finished flag mapping,
error handling (submit_input failures), stream timeout, and session-start
guard (fires once per session, resets after session ends).
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from reach_layer_base import DoneEvent, SentenceEvent, TextChannelBase
from src.mcp_reach import McpReachLayer
from src.server import _handle_call_tool, create_app


def _make_config(port: int | None = None, tool_timeout_s: float | None = None) -> dict:
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
        """Verify that a DoneEvent carrying error fields propagates them instead of returning None."""
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

        app = create_app(layer, _make_config())

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/call_tool", json={"session_id": "multi-1", "text": "turn 1"})
            await client.post("/call_tool", json={"session_id": "multi-1", "text": "turn 2"})

        # on_session_start must have fired exactly once for session "multi-1"
        layer.on_session_start.assert_awaited_once_with("multi-1", "")

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
            # First call ends the session; second call is a fresh reconnect.
            yield DoneEvent(
                turn_status="completed",
                session_ended=(call_count == 1),
            )

        layer.subscribe_events = fake_subscribe

        app = create_app(layer, _make_config())

        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp1 = await client.post(
                "/call_tool", json={"session_id": "reuse-1", "text": "first"}
            )
            assert resp1.json()["finished"] is True

            # Same session_id — should trigger on_session_start again
            await client.post(
                "/call_tool", json={"session_id": "reuse-1", "text": "reconnect"}
            )

        assert layer.on_session_start.await_count == 2
