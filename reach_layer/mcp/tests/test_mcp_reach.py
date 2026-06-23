"""Tests for McpReachLayer and the MCP server.

Covers constructor behavior, lifecycle logs, run_loop no-op,
and _handle_call_tool SSE stream aggregation and finished flag mapping.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from reach_layer_base import DoneEvent, SentenceEvent, TextChannelBase
from src.mcp_reach import McpReachLayer
from src.server import _handle_call_tool


def _make_config(port: int | None = None) -> dict:
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
    return cfg


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
# server._handle_call_tool unit tests
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
