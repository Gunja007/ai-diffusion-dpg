"""
agent_core/tests/test_http_clients.py

Unit tests for all five HTTP client wrappers in src/http_clients/.
All httpx network calls are mocked — no real HTTP traffic.

Clients under test:
  - MemoryLayerHttpClient   (src/http_clients/memory_layer.py)
  - TrustLayerHttpClient    (src/http_clients/trust_layer.py)
  - LearningLayerHttpClient (src/http_clients/learning_layer.py)
  - HttpKnowledgeEngineClient (src/http_clients/knowledge_engine.py)
  - ActionGatewayHttpClient (src/http_clients/action_gateway.py)

Coverage:
  - Normal: successful HTTP response mapped to correct return type
  - Failure: TimeoutException → safe fallback returned, no raise
  - Failure: HTTPStatusError → safe fallback returned, no raise
  - Failure: generic Exception → safe fallback returned, no raise
  - Init: None config raises ValueError
  - Validation: None required param raises ValueError
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import httpx

from src.http_clients.memory_layer import MemoryLayerHttpClient
from src.http_clients.trust_layer import TrustLayerHttpClient
from src.http_clients.learning_layer import LearningLayerHttpClient
from src.http_clients.knowledge_engine import HttpKnowledgeEngineClient
from src.http_clients.action_gateway import ActionGatewayHttpClient
from src.models import SessionState, TrustCheckResult, TurnEvent, ToolCall, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "memory_client": {"endpoint": "http://localhost:8002", "timeout_ms": 3000},
    "trust_client": {"endpoint": "http://localhost:8003", "timeout_ms": 2000},
    "learning_client": {"endpoint": "http://localhost:8004", "timeout_ms": 2000},
    "ke_client": {"endpoint": "http://localhost:8001/assemble_prompt", "timeout_ms": 8000},
    "action_gateway_client": {"endpoint": "http://localhost:9999/onest/market_lookup", "timeout_ms": 5000},
}


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock httpx Response object."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _mock_http_error(status_code: int = 500) -> httpx.HTTPStatusError:
    """Build a mock httpx.HTTPStatusError."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    return httpx.HTTPStatusError("error", request=MagicMock(), response=resp)


def _empty_session(session_id: str = "s1") -> SessionState:
    return SessionState.empty(session_id)


# ===========================================================================
# MemoryLayerHttpClient
# ===========================================================================

class TestMemoryLayerHttpClientInit:
    def test_none_config_raises(self):
        with pytest.raises(ValueError, match="config must not be None"):
            MemoryLayerHttpClient(None)

    def test_defaults_used_when_keys_absent(self):
        client = MemoryLayerHttpClient({})
        assert client._endpoint == "http://localhost:8002"


class TestMemoryReadSession:
    def test_success_returns_session_state(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({
            "session_id": "s1", "history": [], "confirmed_entities": {}, "workflow_step": None, "user_profile": {}
        })):
            state = client.read_session("s1")
        assert isinstance(state, SessionState)
        assert state.session_id == "s1"

    def test_history_populated_from_response(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        history = [{"role": "user", "content": "hello"}]
        with patch("httpx.post", return_value=_mock_response({
            "session_id": "s1", "history": history, "confirmed_entities": {}, "workflow_step": None, "user_profile": {}
        })):
            state = client.read_session("s1")
        assert state.history == history

    def test_timeout_returns_empty_state(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            state = client.read_session("s1")
        assert state.history == []

    def test_http_error_returns_empty_state(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=_mock_http_error(500)):
            state = client.read_session("s1")
        assert isinstance(state, SessionState)

    def test_generic_exception_returns_empty_state(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=RuntimeError("boom")):
            state = client.read_session("s1")
        assert isinstance(state, SessionState)

    def test_none_session_id_raises(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError, match="session_id must not be None"):
            client.read_session(None)


class TestMemoryWriteSession:
    def test_success_does_not_raise(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({"status": "ok"})):
            client.write_session("s1", _empty_session("s1"))  # must not raise

    def test_timeout_does_not_raise(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            client.write_session("s1", _empty_session("s1"))

    def test_http_error_does_not_raise(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=_mock_http_error(503)):
            client.write_session("s1", _empty_session("s1"))

    def test_none_session_id_raises(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError):
            client.write_session(None, _empty_session("s1"))

    def test_none_state_raises(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError):
            client.write_session("s1", None)


class TestMemoryGetUserProfile:
    def test_success_returns_dict(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.get", return_value=_mock_response({"trade": "electrician"})):
            profile = client.get_user_profile("s1")
        assert profile == {"trade": "electrician"}

    def test_timeout_returns_empty_dict(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.get", side_effect=httpx.TimeoutException("timeout")):
            profile = client.get_user_profile("s1")
        assert profile == {}

    def test_non_dict_response_returns_empty(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.get", return_value=_mock_response("not_a_dict")):
            profile = client.get_user_profile("s1")
        assert profile == {}


class TestMemoryClearSession:
    def test_success_does_not_raise(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.delete", return_value=_mock_response({"status": "ok"})):
            client.clear_session("s1")

    def test_timeout_does_not_raise(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.delete", side_effect=httpx.TimeoutException("timeout")):
            client.clear_session("s1")

    def test_none_session_id_raises(self):
        client = MemoryLayerHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError):
            client.clear_session(None)


# ===========================================================================
# TrustLayerHttpClient
# ===========================================================================

class TestTrustLayerHttpClientInit:
    def test_none_config_raises(self):
        with pytest.raises(ValueError, match="config must not be None"):
            TrustLayerHttpClient(None)


class TestTrustCheckInput:
    def test_success_returns_trust_check_result(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({"passed": True, "action": "allow", "reason": None})):
            result = client.check_input("s1", "hello")
        assert isinstance(result, TrustCheckResult)
        assert result.passed is True
        assert result.action == "allow"

    def test_blocked_response_returned_correctly(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({"passed": False, "action": "block", "reason": "harmful"})):
            result = client.check_input("s1", "bad input")
        assert result.passed is False
        assert result.action == "block"

    def test_timeout_fails_open(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            result = client.check_input("s1", "hello")
        assert result.passed is True
        assert result.action == "allow"

    def test_http_error_fails_open(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=_mock_http_error(500)):
            result = client.check_input("s1", "hello")
        assert result.passed is True

    def test_generic_error_fails_open(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=RuntimeError("boom")):
            result = client.check_input("s1", "hello")
        assert result.passed is True

    def test_none_session_id_raises(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError):
            client.check_input(None, "hello")


class TestTrustCheckOutput:
    def test_success_returns_allow(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({"passed": True, "action": "allow", "reason": None})):
            result = client.check_output("s1", "Good response.")
        assert result.passed is True

    def test_timeout_fails_open(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            result = client.check_output("s1", "response")
        assert result.passed is True

    def test_none_session_id_raises(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError):
            client.check_output(None, "resp")


class TestTrustCheckConsent:
    def test_success_returns_bool(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({"granted": True})):
            result = client.check_consent("s1", "job_apply")
        assert result is True

    def test_timeout_fails_open(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            result = client.check_consent("s1", "job_apply")
        assert result is True

    def test_none_session_id_raises(self):
        client = TrustLayerHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError):
            client.check_consent(None, "connector")


# ===========================================================================
# LearningLayerHttpClient
# ===========================================================================

class TestLearningLayerHttpClientInit:
    def test_none_config_raises(self):
        with pytest.raises(ValueError, match="config must not be None"):
            LearningLayerHttpClient(None)


class TestLearningEmitTurn:
    def _make_event(self) -> dict:
        return {
            "session_id": "s1",
            "response_text": "response",
            "tool_calls": [],
            "trust_input_result": {"passed": True, "action": "allow", "reason": None},
            "trust_output_result": {"passed": True, "action": "allow", "reason": None},
            "model_used": "claude-haiku-4-5-20251001",
            "input_tokens": 100,
            "output_tokens": 50,
            "latency_ms": 300,
            "timestamp_ms": 1700000000,
        }

    def test_success_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({"status": "ok"})):
            client.emit_turn(self._make_event())

    def test_none_event_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        client.emit_turn(None)  # must not raise

    def test_timeout_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            client.emit_turn(self._make_event())

    def test_http_error_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=_mock_http_error(500)):
            client.emit_turn(self._make_event())

    def test_generic_exception_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=RuntimeError("boom")):
            client.emit_turn(self._make_event())


class TestLearningEmitSignal:
    def test_success_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({"status": "ok"})):
            client.emit_signal("drop_off", {"turn": 3})

    def test_none_signal_type_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        client.emit_signal(None, {})  # must not raise

    def test_timeout_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            client.emit_signal("test", {})

    def test_generic_exception_does_not_raise(self):
        client = LearningLayerHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=RuntimeError("boom")):
            client.emit_signal("test", {})


# ===========================================================================
# HttpKnowledgeEngineClient
# ===========================================================================

class TestKEHttpClientInit:
    def test_none_config_raises(self):
        with pytest.raises(ValueError, match="config must not be None"):
            HttpKnowledgeEngineClient(None)


class TestKEAssemblePrompt:
    def test_success_returns_messages_and_system(self):
        client = HttpKnowledgeEngineClient(_BASE_CONFIG)
        with patch("httpx.post", return_value=_mock_response({
            "messages": [{"role": "user", "content": "hello"}],
            "system": "You are KKB.",
        })):
            messages, system = client.assemble_prompt(
                session_id="s1",
                user_message="hello",
                session_state=_empty_session("s1"),
            )
        assert isinstance(messages, list)
        assert len(messages) == 1
        assert system == "You are KKB."

    def test_empty_user_message_returns_empty(self):
        client = HttpKnowledgeEngineClient(_BASE_CONFIG)
        messages, system = client.assemble_prompt("s1", "", _empty_session("s1"))
        assert messages == []
        assert system == ""

    def test_timeout_returns_empty(self):
        client = HttpKnowledgeEngineClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            messages, system = client.assemble_prompt("s1", "hello", _empty_session("s1"))
        assert messages == []
        assert system == ""

    def test_http_error_returns_empty(self):
        client = HttpKnowledgeEngineClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=_mock_http_error(503)):
            messages, system = client.assemble_prompt("s1", "hello", _empty_session("s1"))
        assert messages == []

    def test_generic_exception_returns_empty(self):
        client = HttpKnowledgeEngineClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=RuntimeError("boom")):
            messages, system = client.assemble_prompt("s1", "hello", _empty_session("s1"))
        assert messages == []

    def test_nlu_params_forwarded_in_payload(self):
        client = HttpKnowledgeEngineClient(_BASE_CONFIG)
        captured = {}

        def mock_post(url, json=None, timeout=None):
            captured.update(json or {})
            return _mock_response({"messages": [], "system": ""})

        with patch("httpx.post", side_effect=mock_post):
            client.assemble_prompt(
                session_id="s1",
                user_message="kaam chahiye",
                session_state=_empty_session("s1"),
                normalised_input="kaam chahiye",
                intent="market_truth_query",
                entities={"location": "Hubli"},
            )
        assert captured["intent"] == "market_truth_query"
        assert captured["entities"] == {"location": "Hubli"}


# ===========================================================================
# ActionGatewayHttpClient
# ===========================================================================

class TestActionGatewayHttpClientInit:
    def test_none_config_raises(self):
        with pytest.raises(ValueError, match="config must not be None"):
            ActionGatewayHttpClient(None)


class TestActionGatewayListTools:
    def test_returns_list_of_dicts(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        tools = client.list_available_tools()
        assert isinstance(tools, list)
        assert len(tools) >= 1
        assert "name" in tools[0]

    def test_onest_tool_present(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        names = [t["name"] for t in client.list_available_tools()]
        assert "onest_market_lookup" in names


class TestActionGatewayExecute:
    def _make_tool_call(self) -> ToolCall:
        return ToolCall(
            tool_name="onest_market_lookup",
            tool_use_id="tu_1",
            input_params={"trade": "electrician", "location": "Hubli"},
        )

    def test_success_returns_tool_result(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        mock_data = {"salary_range": "12000-18000", "market_signal": "growing"}
        with patch("httpx.post", return_value=_mock_response(mock_data)):
            result = client.execute(self._make_tool_call(), "s1")
        assert isinstance(result, ToolResult)
        assert result.success is True
        assert result.result == mock_data

    def test_timeout_returns_failure_result(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            result = client.execute(self._make_tool_call(), "s1")
        assert result.success is False
        assert "timeout" in result.error.lower()

    def test_http_error_returns_failure_result(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=_mock_http_error(503)):
            result = client.execute(self._make_tool_call(), "s1")
        assert result.success is False

    def test_generic_exception_returns_failure_result(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        with patch("httpx.post", side_effect=RuntimeError("boom")):
            result = client.execute(self._make_tool_call(), "s1")
        assert result.success is False

    def test_unknown_tool_returns_failure_result(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        unknown = ToolCall(tool_name="unknown_tool", tool_use_id="tu_x", input_params={})
        result = client.execute(unknown, "s1")
        assert result.success is False
        assert "unknown_tool" in result.error

    def test_none_tool_call_raises(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError):
            client.execute(None, "s1")

    def test_none_session_id_raises(self):
        client = ActionGatewayHttpClient(_BASE_CONFIG)
        with pytest.raises(ValueError):
            client.execute(self._make_tool_call(), None)
