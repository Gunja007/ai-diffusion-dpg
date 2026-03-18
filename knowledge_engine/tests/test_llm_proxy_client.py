"""
tests/test_llm_proxy_client.py

Unit tests for HttpLLMWrapper.
All httpx calls are mocked — no real HTTP requests are made.
"""

import pytest
import httpx
from unittest.mock import MagicMock, patch

from src.llm_proxy_client import HttpLLMWrapper
from src.models import LLMResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROXY_URL = "http://localhost:8000/internal/llm/call"
MESSAGES = [{"role": "user", "content": "hello"}]


@pytest.fixture
def wrapper():
    return HttpLLMWrapper(proxy_url=PROXY_URL, timeout_ms=5000)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_init_empty_proxy_url_raises():
    with pytest.raises(ValueError, match="proxy_url must not be empty"):
        HttpLLMWrapper(proxy_url="", timeout_ms=5000)


def test_init_zero_timeout_raises():
    with pytest.raises(ValueError, match="timeout_ms must be a positive integer"):
        HttpLLMWrapper(proxy_url=PROXY_URL, timeout_ms=0)


def test_init_negative_timeout_raises():
    with pytest.raises(ValueError, match="timeout_ms must be a positive integer"):
        HttpLLMWrapper(proxy_url=PROXY_URL, timeout_ms=-100)


def test_get_active_model_returns_proxy(wrapper):
    assert wrapper.get_active_model() == "proxy"


# ---------------------------------------------------------------------------
# Normal execution
# ---------------------------------------------------------------------------


def test_call_success_returns_llm_response(wrapper):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "content": "Hello there!",
        "stop_reason": "end_turn",
        "model_used": "claude-haiku-4-5-20251001",
        "input_tokens": 10,
        "output_tokens": 5,
    }

    with patch("httpx.post", return_value=mock_response) as mock_post:
        result = wrapper.call(messages=MESSAGES)

    assert isinstance(result, LLMResponse)
    assert result.content == "Hello there!"
    assert result.stop_reason == "end_turn"
    assert result.model_used == "claude-haiku-4-5-20251001"
    assert result.input_tokens == 10
    assert result.output_tokens == 5


def test_call_passes_correct_payload(wrapper):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "content": "ok",
        "stop_reason": "end_turn",
        "model_used": "proxy",
        "input_tokens": 0,
        "output_tokens": 0,
    }

    with patch("httpx.post", return_value=mock_response) as mock_post:
        wrapper.call(
            messages=MESSAGES,
            tools=[{"name": "tool1"}],
            system="You are helpful.",
            model_override="claude-haiku-4-5-20251001",
        )

    called_kwargs = mock_post.call_args
    payload = called_kwargs[1]["json"]
    assert payload["messages"] == MESSAGES
    assert payload["tools"] == [{"name": "tool1"}]
    assert payload["system"] == "You are helpful."
    assert payload["model_override"] == "claude-haiku-4-5-20251001"


def test_call_default_tools_is_empty_list(wrapper):
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "content": "ok",
        "stop_reason": "end_turn",
        "model_used": "proxy",
        "input_tokens": 0,
        "output_tokens": 0,
    }

    with patch("httpx.post", return_value=mock_response) as mock_post:
        wrapper.call(messages=MESSAGES)

    payload = mock_post.call_args[1]["json"]
    assert payload["tools"] == []
    assert payload["system"] == ""
    assert payload["model_override"] is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_call_empty_messages_raises(wrapper):
    with pytest.raises(ValueError, match="messages must not be empty"):
        wrapper.call(messages=[])


def test_call_missing_optional_fields_in_response(wrapper):
    """Response with only required 'content' — all others default gracefully."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"content": "minimal"}

    with patch("httpx.post", return_value=mock_response):
        result = wrapper.call(messages=MESSAGES)

    assert result.content == "minimal"
    assert result.stop_reason == "end_turn"
    assert result.model_used == "proxy"
    assert result.input_tokens == 0
    assert result.output_tokens == 0


# ---------------------------------------------------------------------------
# Failure scenarios
# ---------------------------------------------------------------------------


def test_call_timeout_returns_error_response(wrapper):
    with patch("httpx.post", side_effect=httpx.TimeoutException("timed out")):
        result = wrapper.call(messages=MESSAGES)

    assert result.stop_reason == "error"
    assert result.content is None


def test_call_http_500_returns_error_response(wrapper):
    mock_response = MagicMock()
    mock_response.status_code = 500
    http_error = httpx.HTTPStatusError(
        "Server Error",
        request=MagicMock(),
        response=mock_response,
    )
    mock_response.raise_for_status.side_effect = http_error

    with patch("httpx.post", return_value=mock_response):
        result = wrapper.call(messages=MESSAGES)

    assert result.stop_reason == "error"
    assert result.content is None


def test_call_http_422_returns_error_response(wrapper):
    mock_response = MagicMock()
    mock_response.status_code = 422
    http_error = httpx.HTTPStatusError(
        "Unprocessable Entity",
        request=MagicMock(),
        response=mock_response,
    )
    mock_response.raise_for_status.side_effect = http_error

    with patch("httpx.post", return_value=mock_response):
        result = wrapper.call(messages=MESSAGES)

    assert result.stop_reason == "error"
    assert result.content is None


def test_call_connection_error_returns_error_response(wrapper):
    with patch("httpx.post", side_effect=httpx.ConnectError("connection refused")):
        result = wrapper.call(messages=MESSAGES)

    assert result.stop_reason == "error"
    assert result.content is None


def test_call_unexpected_exception_returns_error_response(wrapper):
    with patch("httpx.post", side_effect=RuntimeError("unexpected")):
        result = wrapper.call(messages=MESSAGES)

    assert result.stop_reason == "error"
    assert result.content is None


def test_call_uses_configured_timeout(wrapper):
    """Verify the timeout is passed to httpx in seconds (converted from ms)."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "content": "ok",
        "stop_reason": "end_turn",
        "model_used": "proxy",
        "input_tokens": 0,
        "output_tokens": 0,
    }

    with patch("httpx.post", return_value=mock_response) as mock_post:
        wrapper.call(messages=MESSAGES)

    called_timeout = mock_post.call_args[1]["timeout"]
    assert called_timeout == 5.0  # 5000ms → 5.0s
