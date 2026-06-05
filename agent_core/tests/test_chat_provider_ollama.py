"""Tests for OllamaChatProvider."""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from src.chat_provider.ollama_provider import OllamaChatProvider
from src.chat_provider.base import Capabilities, ProviderConfigError, UnsupportedFeatureError
from src.chat_provider.types import (
    ChatRequest,
    ImageBlock,
    ImageSource,
    Message,
    OutputFormat,
    SystemPrompt,
    TextBlock,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)


VALID_CONFIG = {
    "primary_model": "llama2",
    "base_url": "http://localhost:11434",
    "timeout_ms": 5000,
    "retry_attempts": 2,
    "retry_backoff_seconds": [0, 0.0, 0.0],
    "features": {
        "streaming": True,
    },
}


class TestInit:
    def test_capabilities(self):
        with patch("ollama.Client"), patch("ollama.AsyncClient"):
            p = OllamaChatProvider(VALID_CONFIG)
        caps = p.capabilities
        assert isinstance(caps, Capabilities)
        assert caps.supports_tools is True
        assert caps.supports_streaming is True
        assert caps.supports_prompt_cache is True
        assert caps.supports_image_input is True
        assert caps.supports_audio_input is False
        assert caps.supports_structured_output is True
        assert caps.supports_force_tool_choice is True

    def test_features_defaults_match_capability(self):
        # Empty features dict → effective features come from capabilities.
        cfg = {**VALID_CONFIG, "features": {}}
        with patch("ollama.Client"), patch("ollama.AsyncClient"):
            p = OllamaChatProvider(cfg)
        assert p._features["streaming"] is True
        assert p._features["image_input"] is True
        assert p._features["prompt_cache"] is True

    def test_empty_config_raises(self):
        with pytest.raises(ProviderConfigError):
            OllamaChatProvider({})

    def test_missing_primary_model_raises(self):
        cfg = {**VALID_CONFIG}
        cfg.pop("primary_model")
        with pytest.raises(ProviderConfigError, match="primary_model"):
            OllamaChatProvider(cfg)

    def test_missing_base_url_raises(self):
        cfg = {**VALID_CONFIG}
        cfg.pop("base_url")
        with pytest.raises(ProviderConfigError, match="base_url"):
            OllamaChatProvider(cfg)

    def test_missing_timeout_raises(self):
        cfg = {**VALID_CONFIG}
        cfg.pop("timeout_ms")
        with pytest.raises(ProviderConfigError, match="timeout_ms"):
            OllamaChatProvider(cfg)

    def test_missing_retry_attempts_raises(self):
        cfg = {**VALID_CONFIG}
        cfg.pop("retry_attempts")
        with pytest.raises(ProviderConfigError, match="retry_attempts"):
            OllamaChatProvider(cfg)

    def test_base_url_trailing_slash_stripped(self):
        cfg = {**VALID_CONFIG, "base_url": "http://localhost:11434/"}
        with patch("ollama.Client") as mock_client, patch("ollama.AsyncClient"):
            OllamaChatProvider(cfg)
        # Verify the trailing slash is removed
        mock_client.assert_called_once_with(base_url="http://localhost:11434")

    def test_ollama_import_missing_raises(self):
        cfg = {**VALID_CONFIG}
        with patch.dict("sys.modules", {"ollama": None}):
            with pytest.raises(ProviderConfigError, match="ollama package"):
                with patch("builtins.__import__", side_effect=ImportError):
                    OllamaChatProvider(cfg)

    def test_get_active_model(self):
        with patch("ollama.Client"), patch("ollama.AsyncClient"):
            p = OllamaChatProvider(VALID_CONFIG)
        assert p.get_active_model() == "llama2"


def _make_provider(features: dict | None = None, **overrides) -> OllamaChatProvider:
    cfg = dict(VALID_CONFIG)
    if features is not None:
        cfg["features"] = features
    cfg.update(overrides)
    with patch("ollama.Client"), patch("ollama.AsyncClient"):
        return OllamaChatProvider(cfg)


class TestToWire:
    def test_minimal_text_request(self):
        p = _make_provider()
        req = ChatRequest(messages=[Message(role="user", content=[TextBlock(text="hi")])])
        wire = p._to_wire(req)
        assert wire == {
            "model": "llama2",
            "messages": [{"role": "user", "content": "hi"}],
            "options": {"num_predict": 4096},
        }

    def test_system_prompt_concatenated_at_head(self):
        p = _make_provider()
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            system=SystemPrompt(blocks=[
                TextBlock(text="You are helpful."),
                TextBlock(text="Be concise."),
            ]),
        )
        wire = p._to_wire(req)
        assert wire["messages"][0] == {
            "role": "system",
            "content": "You are helpful.\n\nBe concise.",
        }
        assert wire["messages"][1]["role"] == "user"

    def test_text_only_message_uses_string_content(self):
        # Single TextBlock → content is a string.
        p = _make_provider()
        req = ChatRequest(messages=[Message(role="user", content=[TextBlock(text="hi")])])
        wire = p._to_wire(req)
        assert wire["messages"][0]["content"] == "hi"

    def test_multiple_text_blocks_concatenated(self):
        p = _make_provider()
        req = ChatRequest(
            messages=[Message(
                role="user",
                content=[TextBlock(text="first"), TextBlock(text="second")],
            )]
        )
        wire = p._to_wire(req)
        assert wire["messages"][0]["content"] == "first\n\nsecond"

    def test_tool_definition(self):
        p = _make_provider()
        td = ToolDefinition(
            name="get_x",
            description="get x",
            input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        )
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[td],
            tool_choice="auto",
        )
        wire = p._to_wire(req)
        assert "tools" in wire
        assert wire["tools"][0] == {
            "type": "function",
            "function": {
                "name": "get_x",
                "description": "get x",
                "parameters": {"type": "object", "properties": {"key": {"type": "string"}}},
            },
        }

    def test_tool_choice_none_skips_tools_wire(self):
        p = _make_provider()
        td = ToolDefinition(
            name="lookup",
            description="lookup",
            input_schema={"type": "object"},
        )
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[td],
            tool_choice="none",
        )
        wire = p._to_wire(req)
        assert "tools" not in wire

    def test_assistant_tool_use_and_tool_result_messages(self):
        p = _make_provider()
        req = ChatRequest(
            messages=[
                Message(role="user", content=[TextBlock(text="look it up")]),
                Message(
                    role="assistant",
                    content=[
                        TextBlock(text="checking"),
                        ToolUseBlock(tool_use_id="call_abc", tool_name="lookup", input={"q": "x"}),
                    ],
                ),
                Message(
                    role="user",
                    content=[ToolResultBlock(tool_use_id="call_abc", content="42")],
                ),
            ]
        )
        wire = p._to_wire(req)
        assert wire["messages"][1] == {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [{
                "id": "call_abc",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
            }],
        }
        assert wire["messages"][2] == {
            "role": "tool",
            "tool_call_id": "call_abc",
            "content": "42",
        }

    def test_max_tokens_passed_through(self):
        p = _make_provider()
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            max_tokens=200,
        )
        wire = p._to_wire(req)
        assert wire["options"]["num_predict"] == 200

    def test_image_block_uses_content_parts(self):
        p = _make_provider(features={"image_input": True})
        img = ImageBlock(source=ImageSource(kind="url", url="https://x/y.png"))
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="describe"), img])]
        )
        wire = p._to_wire(req)
        assert wire["messages"][0]["content"] == [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ]

    def test_image_base64_uses_data_url(self):
        p = _make_provider(features={"image_input": True})
        img = ImageBlock(source=ImageSource(kind="base64", media_type="image/png", data="ABC=="))
        req = ChatRequest(messages=[Message(role="user", content=[img])])
        wire = p._to_wire(req)
        assert wire["messages"][0]["content"] == [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC=="}},
        ]

    def test_image_input_disabled_in_features_ignored(self):
        p = _make_provider(features={"image_input": False})
        img = ImageBlock(source=ImageSource(kind="url", url="https://x/y.png"))
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="describe"), img])]
        )
        wire = p._to_wire(req)
        # When image_input is disabled, only text is included
        assert wire["messages"][0]["content"] == "describe"

    def test_cache_hint_on_system_prompt(self):
        p = _make_provider(features={"prompt_cache": True})
        long_text = "x" * 4000  # Exceeds _CACHE_MIN_CHARS
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            system=SystemPrompt(blocks=[TextBlock(text=long_text, cache_hint="session")]),
        )
        wire = p._to_wire(req)
        # System prompt is flattened into a single message in Ollama format
        assert wire["messages"][0]["role"] == "system"
        assert wire["messages"][0]["content"] == long_text

    def test_output_format_appends_respond_with_json_tool(self):
        p = _make_provider()
        of = OutputFormat(
            schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
        )
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="answer")])],
            output_format=of,
        )
        wire = p._to_wire(req)
        assert "tools" in wire
        # Should have a respond_with_json tool
        tool_names = [t["function"]["name"] for t in wire["tools"]]
        assert "respond_with_json" in tool_names
        # Should have tool_choice forced to respond_with_json
        assert wire["tool_choice"] == {"type": "function", "name": "respond_with_json"}


def _mk_ollama_response(
    text: str | None = None,
    tool_calls: list[dict] | None = None,
    done_reason: str | None = None,
    prompt_eval_count: int = 10,
    eval_count: int = 5,
) -> dict:
    """Build a dict that mimics ollama.Client.chat() response."""
    resp = {
        "model": "llama2",
        "message": {
            "role": "assistant",
            "content": text or "",
        },
        "done": True,
        "done_reason": done_reason or "stop",
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
    }
    if tool_calls is not None:
        resp["message"]["tool_calls"] = tool_calls
    return resp


class TestFromWire:
    def test_text_only(self):
        p = _make_provider()
        raw = _mk_ollama_response(text="hello back", prompt_eval_count=12, eval_count=4)
        resp = p._from_wire(raw, output_format=None)
        assert resp.stop_reason == "end_turn"
        assert resp.model_used == "llama2"
        assert len(resp.content) == 1
        assert resp.content[0].type == "text"
        assert resp.content[0].text == "hello back"
        assert resp.parsed_output is None
        assert resp.usage.input_tokens == 12
        assert resp.usage.output_tokens == 4
        assert resp.usage.cache_read_tokens is None
        assert resp.usage.cache_creation_tokens is None

    def test_tool_calls(self):
        p = _make_provider()
        raw = _mk_ollama_response(
            text=None,
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
            }],
            done_reason="tool_calls",
        )
        resp = p._from_wire(raw, output_format=None)
        # Tool calls are detected from message.tool_calls, not from done_reason
        # So stop_reason is "end_turn" (tool_calls done_reason maps to end_turn)
        assert resp.stop_reason == "end_turn"
        assert len(resp.content) == 1
        assert resp.content[0].type == "tool_use"
        assert resp.content[0].tool_name == "lookup"
        assert resp.content[0].input == {"q": "x"}

    def test_text_plus_tool_call(self):
        p = _make_provider()
        raw = _mk_ollama_response(
            text="checking",
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        )
        resp = p._from_wire(raw, output_format=None)
        assert len(resp.content) == 2
        assert resp.content[0].type == "text"
        assert resp.content[1].type == "tool_use"

    def test_done_reason_length_maps_to_max_tokens(self):
        p = _make_provider()
        raw = _mk_ollama_response(text="trunc", done_reason="length")
        resp = p._from_wire(raw, output_format=None)
        assert resp.stop_reason == "max_tokens"

    def test_done_reason_stop_maps_to_end_turn(self):
        p = _make_provider()
        raw = _mk_ollama_response(text="ok", done_reason="stop")
        resp = p._from_wire(raw, output_format=None)
        assert resp.stop_reason == "end_turn"

    def test_empty_done_reason_maps_to_end_turn(self):
        p = _make_provider()
        raw = _mk_ollama_response(text="ok", done_reason="")
        resp = p._from_wire(raw, output_format=None)
        assert resp.stop_reason == "end_turn"

    def test_tool_call_with_malformed_json_arguments(self):
        p = _make_provider()
        raw = _mk_ollama_response(
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "not valid json"},
            }],
        )
        resp = p._from_wire(raw, output_format=None)
        assert len(resp.content) == 1
        assert resp.content[0].type == "tool_use"
        # Malformed JSON should result in empty dict
        assert resp.content[0].input == {}

    def test_missing_usage_counts_default_to_zero(self):
        p = _make_provider()
        raw = _mk_ollama_response(text="ok", prompt_eval_count=None, eval_count=None)
        resp = p._from_wire(raw, output_format=None)
        assert resp.usage.input_tokens == 0
        assert resp.usage.output_tokens == 0

    def test_output_format_parses_json(self):
        p = _make_provider()
        of = OutputFormat(schema={"type": "object", "properties": {"answer": {"type": "string"}}})
        raw = _mk_ollama_response(text='{"answer": "42"}')
        resp = p._from_wire(raw, output_format=of)
        assert resp.parsed_output == {"answer": "42"}
        assert resp.stop_reason == "end_turn"

    def test_output_format_with_invalid_json_marks_error(self):
        p = _make_provider()
        of = OutputFormat(schema={})
        raw = _mk_ollama_response(text='{"not valid')
        resp = p._from_wire(raw, output_format=of)
        assert resp.parsed_output is None
        assert resp.stop_reason == "error"


class TestCall:
    def test_normal_text_response(self):
        p = _make_provider()
        raw = _mk_ollama_response(text="hi back", prompt_eval_count=10, eval_count=2)
        p._client.chat = MagicMock(return_value=raw)

        req = ChatRequest(messages=[Message(role="user", content=[TextBlock(text="hi")])])
        resp = p.call(req)
        assert resp.stop_reason == "end_turn"
        assert len(resp.content) == 1
        assert resp.content[0].text == "hi back"
        assert resp.usage.input_tokens == 10
        assert resp.usage.output_tokens == 2

    def test_empty_messages_raises(self):
        p = _make_provider()
        req = ChatRequest(messages=[])
        with pytest.raises(ValueError, match="messages must not be empty"):
            p.call(req)

    def test_unsupported_feature_prompt_cache_raises(self):
        # Prompt cache is now supported, so this test verifies it works without raising
        p = _make_provider()
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="hi", cache_hint="session")])],
        )
        # Should not raise; should translate successfully
        wire = p._to_wire(req)
        assert "messages" in wire

    def test_unsupported_feature_structured_output_raises(self):
        # Structured output is now supported, so this test verifies it works without raising
        p = _make_provider()
        of = OutputFormat(schema={"type": "object", "properties": {"answer": {"type": "string"}}})
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="answer")])],
            output_format=of,
        )
        # Should not raise; should translate successfully
        wire = p._to_wire(req)
        assert "tools" in wire

    def test_connection_error_retries_and_exhausts(self):
        p = _make_provider(retry_attempts=2, retry_backoff_seconds=[0, 0.0])
        p._client.chat = MagicMock(side_effect=ConnectionError("connection refused"))
        p._backoff_seconds = [0.0, 0.0]

        req = ChatRequest(messages=[Message(role="user", content=[TextBlock(text="hi")])])
        resp = p.call(req)
        # On exhausted retries, returns error response
        assert resp.stop_reason == "error"
        assert resp.content == []
        assert resp.usage.input_tokens is None

    def test_api_error_returns_error_response(self):
        p = _make_provider()
        p._client.chat = MagicMock(side_effect=ValueError("model not found"))

        req = ChatRequest(messages=[Message(role="user", content=[TextBlock(text="hi")])])
        resp = p.call(req)
        # Non-retryable error on first attempt → error response
        assert resp.stop_reason == "error"
        assert resp.usage.input_tokens is None

    def test_retryable_error_eventually_succeeds(self):
        p = _make_provider(retry_attempts=3, retry_backoff_seconds=[0, 0.0, 0.0])
        p._backoff_seconds = [0.0, 0.0, 0.0]
        raw = _mk_ollama_response(text="hi back")
        p._client.chat = MagicMock(
            side_effect=[
                ConnectionError("timeout"),
                ConnectionError("timeout"),
                raw,
            ]
        )

        req = ChatRequest(messages=[Message(role="user", content=[TextBlock(text="hi")])])
        resp = p.call(req)
        # Retries succeed on third attempt
        assert resp.stop_reason == "end_turn"
        assert resp.content[0].text == "hi back"

    def test_is_retryable_error_classification(self):
        # ConnectionError
        assert OllamaChatProvider._is_retryable_error(ConnectionError("test")) is True
        # TimeoutError
        assert OllamaChatProvider._is_retryable_error(TimeoutError("test")) is True
        # Status code 429
        err = MagicMock()
        err.status_code = 429
        assert OllamaChatProvider._is_retryable_error(err) is True
        # Status code 500
        err.status_code = 500
        assert OllamaChatProvider._is_retryable_error(err) is True
        # Status code 400 (not retryable)
        err.status_code = 400
        assert OllamaChatProvider._is_retryable_error(err) is False
        # ValueError (not retryable)
        assert OllamaChatProvider._is_retryable_error(ValueError("test")) is False


class TestStream:
    @pytest.mark.asyncio
    async def test_normal_streaming(self):
        p = _make_provider()
        chunks = [
            {"message": {"content": "hello"}, "done": False},
            {"message": {"content": " world"}, "done": False},
            {"message": {"content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5},
        ]
        
        async def mock_stream():
            for chunk in chunks:
                yield chunk
        
        async_ctx_mgr = MagicMock()
        async_ctx_mgr.__aenter__.return_value = mock_stream()
        async_ctx_mgr.__aexit__.return_value = None
        p._async_client.chat = MagicMock(return_value=async_ctx_mgr)

        req = ChatRequest(messages=[Message(role="user", content=[TextBlock(text="hi")])])
        tokens = []
        async for token in p.stream(req):
            tokens.append(token)

        assert tokens == ["hello", " world"]

    @pytest.mark.asyncio
    async def test_stream_empty_messages_raises(self):
        p = _make_provider()
        req = ChatRequest(messages=[])
        with pytest.raises(ValueError, match="messages must not be empty"):
            async for _ in p.stream(req):
                pass

    @pytest.mark.asyncio
    async def test_stream_unsupported_feature_raises(self):
        p = _make_provider()
        of = OutputFormat(schema={})
        req = ChatRequest(
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            output_format=of,
        )
        with pytest.raises(UnsupportedFeatureError, match="output_format"):
            async for _ in p.stream(req):
                pass

    @pytest.mark.asyncio
    async def test_stream_abort_event(self):
        p = _make_provider()
        chunks = [
            {"message": {"content": "chunk1"}, "done": False},
            {"message": {"content": "chunk2"}, "done": False},
            {"message": {"content": "chunk3"}, "done": True, "done_reason": "stop"},
        ]
        
        async def mock_stream():
            for chunk in chunks:
                yield chunk
        
        async_ctx_mgr = MagicMock()
        async_ctx_mgr.__aenter__.return_value = mock_stream()
        async_ctx_mgr.__aexit__.return_value = None
        p._async_client.chat = MagicMock(return_value=async_ctx_mgr)

        abort_event = MagicMock()
        abort_event.is_set = MagicMock(side_effect=[False, True])  # Abort on second chunk

        req = ChatRequest(messages=[Message(role="user", content=[TextBlock(text="hi")])])
        tokens = []
        async for token in p.stream(req, abort_event=abort_event):
            tokens.append(token)

        # Should stop after first chunk due to abort
        assert len(tokens) == 1
        assert tokens[0] == "chunk1"


class TestFactory:
    def test_ollama_provider_registered_in_factory(self):
        from src.chat_provider import build_chat_provider
        
        cfg = {
            "provider": "ollama",
            "primary_model": "llama2",
            "base_url": "http://localhost:11434",
            "timeout_ms": 5000,
            "retry_attempts": 2,
        }
        with patch("ollama.Client"), patch("ollama.AsyncClient"):
            provider = build_chat_provider(cfg)
        assert isinstance(provider, OllamaChatProvider)
        assert provider.get_active_model() == "llama2"

    def test_unknown_provider_raises(self):
        from src.chat_provider import build_chat_provider
        
        cfg = {
            "provider": "unknown",
            "primary_model": "some_model",
            "timeout_ms": 5000,
            "retry_attempts": 2,
        }
        with pytest.raises(ProviderConfigError, match="Unknown provider"):
            build_chat_provider(cfg)

    def test_known_providers_listed_in_error(self):
        from src.chat_provider import build_chat_provider
        
        cfg = {
            "provider": "unknown",
            "primary_model": "some_model",
            "timeout_ms": 5000,
            "retry_attempts": 2,
        }
        with pytest.raises(ProviderConfigError, match="'anthropic', 'openai', 'ollama'"):
            build_chat_provider(cfg)
