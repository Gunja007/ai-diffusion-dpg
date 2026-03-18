"""
reach_layer/tests/test_cli_reach.py

Tests for CLIReachLayer.

stdin is mocked via io.StringIO; stdout is captured via capsys.
"""

from __future__ import annotations

import io
import sys
import pytest
from unittest.mock import patch

from src.cli_reach import CLIReachLayer, TurnInput, TurnResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "reach_layer": {
        "cli": {
            "prompt": "You: ",
            "agent_prefix": "Agent: ",
        }
    }
}


def _make_layer(session_id: str = "test-session") -> CLIReachLayer:
    return CLIReachLayer(_BASE_CONFIG, session_id=session_id)


def _make_result(**kwargs) -> TurnResult:
    defaults = {
        "session_id": "test-session",
        "response_text": "Hello, how can I help?",
        "was_escalated": False,
        "was_tool_used": False,
        "model_used": "claude-test",
        "latency_ms": 100,
    }
    defaults.update(kwargs)
    return TurnResult(**defaults)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

class TestConstructor:
    def test_raises_if_config_is_none(self) -> None:
        with pytest.raises(ValueError, match="config must not be None"):
            CLIReachLayer(None)

    def test_generates_session_id_if_not_provided(self) -> None:
        layer = CLIReachLayer(_BASE_CONFIG)
        assert layer.session_id
        assert len(layer.session_id) > 0

    def test_uses_provided_session_id(self) -> None:
        layer = CLIReachLayer(_BASE_CONFIG, session_id="my-session")
        assert layer.session_id == "my-session"

    def test_uses_default_prompt_when_not_configured(self) -> None:
        layer = CLIReachLayer({})
        assert layer._prompt == "You: "

    def test_reads_prompt_from_config(self) -> None:
        cfg = {"reach_layer": {"cli": {"prompt": ">>> "}}}
        layer = CLIReachLayer(cfg)
        assert layer._prompt == ">>> "


# ---------------------------------------------------------------------------
# receive()
# ---------------------------------------------------------------------------

class TestReceive:
    def test_receive_returns_turn_input(self) -> None:
        layer = _make_layer()
        with patch("sys.stdin", io.StringIO("hello world\n")):
            result = layer.receive()
        assert isinstance(result, TurnInput)

    def test_receive_captures_user_message(self) -> None:
        layer = _make_layer()
        with patch("sys.stdin", io.StringIO("I need a job\n")):
            result = layer.receive()
        assert result.user_message == "I need a job"

    def test_receive_strips_trailing_newline(self) -> None:
        layer = _make_layer()
        with patch("sys.stdin", io.StringIO("  hello  \n")):
            result = layer.receive()
        assert result.user_message == "hello"

    def test_receive_sets_channel_to_cli(self) -> None:
        layer = _make_layer()
        with patch("sys.stdin", io.StringIO("test\n")):
            result = layer.receive()
        assert result.channel == "cli"

    def test_receive_sets_session_id(self) -> None:
        layer = _make_layer(session_id="abc-123")
        with patch("sys.stdin", io.StringIO("test\n")):
            result = layer.receive()
        assert result.session_id == "abc-123"

    def test_receive_sets_timestamp_ms(self) -> None:
        layer = _make_layer()
        with patch("sys.stdin", io.StringIO("test\n")):
            result = layer.receive()
        assert result.timestamp_ms > 0

    def test_receive_raises_eof_on_closed_stdin(self) -> None:
        layer = _make_layer()
        with patch("sys.stdin", io.StringIO("")):
            with pytest.raises(EOFError):
                layer.receive()

    def test_receive_handles_empty_message(self) -> None:
        layer = _make_layer()
        with patch("sys.stdin", io.StringIO("\n")):
            result = layer.receive()
        assert result.user_message == ""


# ---------------------------------------------------------------------------
# deliver()
# ---------------------------------------------------------------------------

class TestDeliver:
    def test_deliver_prints_response(self, capsys: pytest.CaptureFixture) -> None:
        layer = _make_layer()
        layer.deliver(_make_result(response_text="Here is your answer"))
        captured = capsys.readouterr()
        assert "Here is your answer" in captured.out

    def test_deliver_includes_agent_prefix(self, capsys: pytest.CaptureFixture) -> None:
        layer = _make_layer()
        layer.deliver(_make_result(response_text="Hello"))
        captured = capsys.readouterr()
        assert captured.out.startswith("Agent: ")

    def test_deliver_escalated_includes_notice(self, capsys: pytest.CaptureFixture) -> None:
        layer = _make_layer()
        layer.deliver(_make_result(was_escalated=True, response_text="Connecting you to agent"))
        captured = capsys.readouterr()
        assert "ESCALATED" in captured.out

    def test_deliver_none_result_does_not_raise(self) -> None:
        layer = _make_layer()
        # Should not raise — None result is logged and skipped
        layer.deliver(None)

    def test_deliver_empty_response_text(self, capsys: pytest.CaptureFixture) -> None:
        layer = _make_layer()
        layer.deliver(_make_result(response_text=""))
        captured = capsys.readouterr()
        assert "Agent: " in captured.out

    def test_custom_agent_prefix_used(self, capsys: pytest.CaptureFixture) -> None:
        cfg = {"reach_layer": {"cli": {"agent_prefix": "Bot> ", "prompt": "> "}}}
        layer = CLIReachLayer(cfg, session_id="s1")
        layer.deliver(_make_result(response_text="Hi"))
        captured = capsys.readouterr()
        assert "Bot> Hi" in captured.out
