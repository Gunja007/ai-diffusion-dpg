import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import json
from unittest import mock

import pytest

import dev_kit.agent.app as app_mod
from dev_kit.agent.phase_driver import ToolCall


@pytest.fixture()
def setup_openai_provider(monkeypatch):
    monkeypatch.setattr(app_mod, "_devkit_provider", "openai")
    monkeypatch.setattr(app_mod, "_openai_api_key", "test-openai-key")


def test_tool_result_translation(setup_openai_provider):
    messages = [
        {"role": "user", "content": "Hello"},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "some output content"
                },
                {
                    "type": "tool_result",
                    # missing tool_use_id -> should be skipped
                    "content": "skipped content"
                }
            ]
        }
    ]
    
    llm_call = app_mod._build_devkit_llm_call()
    
    mock_choice = mock.MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "response content"
    mock_choice.message.tool_calls = []
    
    mock_response = mock.MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model = "gpt-4o"
    
    with mock.patch("openai.OpenAI") as mock_openai_cls:
        mock_client = mock_openai_cls.return_value
        mock_client.chat.completions.create.return_value = mock_response
        
        llm_call("system prompt", messages)
        
        mock_client.chat.completions.create.assert_called_once()
        called_kwargs = mock_client.chat.completions.create.call_args[1]
        openai_messages = called_kwargs["messages"]
        
        assert openai_messages[0] == {"role": "system", "content": "system prompt"}
        assert openai_messages[1] == {"role": "user", "content": "Hello"}
        assert openai_messages[2] == {"role": "tool", "tool_call_id": "call-1", "content": "some output content"}
        assert len(openai_messages) == 3


def test_assistant_tool_use_replay(setup_openai_provider):
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me call a tool"},
                {
                    "type": "tool_use",
                    "id": "call-2",
                    "name": "update_config",
                    "input": {"path": "my.field", "value": "my-val"}
                }
            ]
        }
    ]
    
    llm_call = app_mod._build_devkit_llm_call()
    
    mock_choice = mock.MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "response"
    mock_choice.message.tool_calls = []
    
    mock_response = mock.MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model = "gpt-4o"
    
    with mock.patch("openai.OpenAI") as mock_openai_cls:
        mock_client = mock_openai_cls.return_value
        mock_client.chat.completions.create.return_value = mock_response
        
        llm_call("", messages)
        
        called_kwargs = mock_client.chat.completions.create.call_args[1]
        openai_messages = called_kwargs["messages"]
        
        assert len(openai_messages) == 1
        assistant_msg = openai_messages[0]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "let me call a tool"
        assert len(assistant_msg["tool_calls"]) == 1
        assert assistant_msg["tool_calls"][0] == {
            "id": "call-2",
            "type": "function",
            "function": {
                "name": "update_config",
                "arguments": '{"path": "my.field", "value": "my-val"}'
            }
        }


@pytest.mark.parametrize(
    ("finish_reason", "expected_stop_reason"),
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "content_filter"),
    ]
)
def test_finish_reason_mappings(setup_openai_provider, finish_reason, expected_stop_reason):
    llm_call = app_mod._build_devkit_llm_call()
    
    mock_choice = mock.MagicMock()
    mock_choice.finish_reason = finish_reason
    mock_choice.message.content = "done"
    mock_choice.message.tool_calls = []
    
    mock_response = mock.MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model = "gpt-4o"
    
    with mock.patch("openai.OpenAI") as mock_openai_cls:
        mock_client = mock_openai_cls.return_value
        mock_client.chat.completions.create.return_value = mock_response
        
        res = llm_call("", [{"role": "user", "content": "hi"}])
        assert res.stop_reason == expected_stop_reason


def test_empty_assistant_guard(setup_openai_provider):
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": []},
        {"role": "user", "content": "World"},
    ]
    
    llm_call = app_mod._build_devkit_llm_call()
    
    mock_choice = mock.MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "response content"
    mock_choice.message.tool_calls = []
    
    mock_response = mock.MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model = "gpt-4o"
    
    with mock.patch("openai.OpenAI") as mock_openai_cls:
        mock_client = mock_openai_cls.return_value
        mock_client.chat.completions.create.return_value = mock_response
        
        llm_call("", messages)
        
        called_kwargs = mock_client.chat.completions.create.call_args[1]
        openai_messages = called_kwargs["messages"]
        
        assert len(openai_messages) == 2
        assert openai_messages[0] == {"role": "user", "content": "Hello"}
        assert openai_messages[1] == {"role": "user", "content": "World"}


def test_json_loads_failure_fallback(setup_openai_provider):
    llm_call = app_mod._build_devkit_llm_call()
    
    mock_tc = mock.MagicMock()
    mock_tc.id = "call-3"
    mock_tc.function.name = "update_config"
    mock_tc.function.arguments = "{bad_json"
    
    mock_choice = mock.MagicMock()
    mock_choice.finish_reason = "tool_calls"
    mock_choice.message.content = None
    mock_tc.id = "call-3"
    mock_choice.message.tool_calls = [mock_tc]
    
    mock_response = mock.MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.model = "gpt-4o"
    
    with mock.patch("openai.OpenAI") as mock_openai_cls:
        mock_client = mock_openai_cls.return_value
        mock_client.chat.completions.create.return_value = mock_response
        
        res = llm_call("", [{"role": "user", "content": "hi"}])
        
        assert len(res.tool_calls) == 1
        assert res.tool_calls[0].name == "update_config"
        assert res.tool_calls[0].id == "call-3"
        assert res.tool_calls[0].args == {}
        
        assert len(res.raw_content) == 1
        assert res.raw_content[0] == {
            "type": "tool_use",
            "id": "call-3",
            "name": "update_config",
            "input": {}
        }


@pytest.fixture()
def setup_google_provider(monkeypatch):
    monkeypatch.setattr(app_mod, "_devkit_provider", "google")
    monkeypatch.setattr(app_mod, "_google_api_key", "test-google-key")


def test_google_devkit_llm_call_success(setup_google_provider):
    messages = [
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Sure, I can help."},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "update_config",
                    "input": {"path": "a.b", "value": "x"}
                }
            ]
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": '{"status": "success"}'
                }
            ]
        }
    ]
    
    mock_part_text = mock.MagicMock()
    mock_part_text.text = "Here is the result."
    mock_part_text.function_call = None
    
    # Mock FinishReason enum
    class FinishReasonMock:
        STOP = "STOP"
        MAX_TOKENS = "MAX_TOKENS"

    mock_candidate = mock.MagicMock()
    mock_candidate.content.parts = [mock_part_text]
    mock_candidate.finish_reason = FinishReasonMock.STOP
    
    mock_response = mock.MagicMock()
    mock_response.candidates = [mock_candidate]
    mock_response.model_version = "gemini-2.5"
    mock_response.usage_metadata.prompt_token_count = 10
    mock_response.usage_metadata.candidates_token_count = 5
    
    with mock.patch("google.genai.Client") as mock_client_cls, \
         mock.patch("google.genai.types.FinishReason", FinishReasonMock):
        mock_client = mock_client_cls.return_value
        mock_client.models.generate_content.return_value = mock_response
        
        llm_call = app_mod._build_devkit_llm_call()
        res = llm_call("google system prompt", messages)
        
        mock_client_cls.assert_called_once_with(api_key="test-google-key", http_options={"timeout": 30.0})
        mock_client.models.generate_content.assert_called_once()
        
        assert res.text == "Here is the result."
        assert res.stop_reason == "end_turn"
        assert res.input_tokens == 10
        assert res.output_tokens == 5
        assert res.model == "gemini-2.5"


def test_google_devkit_llm_call_empty_candidates(setup_google_provider):
    mock_response = mock.MagicMock()
    mock_response.candidates = []
    mock_response.model_version = "gemini-2.5"
    mock_response.usage_metadata.prompt_token_count = 2
    mock_response.usage_metadata.candidates_token_count = 0
    
    with mock.patch("google.genai.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.models.generate_content.return_value = mock_response
        
        llm_call = app_mod._build_devkit_llm_call()
        res = llm_call("google system prompt", [{"role": "user", "content": "hello"}])
        
        assert res.text == "[Response blocked by safety filters]"
        assert res.stop_reason == "error"
        assert res.tool_calls == []
        assert res.input_tokens == 2
        assert res.output_tokens == 0

