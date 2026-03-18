"""
agent_core/tests/test_nlu_processor.py

Unit tests for NLUProcessor (agent_core/src/nlu_processor.py).
All LLM calls are mocked — no real API calls.

Coverage:
- Normal: various intents classified correctly
- Normal: entities extracted from message
- Normal: sentiment detected
- Normal: confidence value returned
- Normal: model_override passed from config to llm.call
- Normal: recent session history injected into LLM messages
- Normal: history_turns config limits how many turns are injected
- Edge: empty input returns NLUResult(intent="unknown", confidence=0.0)
- Edge: invalid intent from LLM falls back to "unknown"
- Edge: non-dict entities from LLM treated as empty dict
- Failure: LLM returns error stop_reason → fallback NLUResult
- Failure: LLM raises exception → fallback NLUResult
- Failure: malformed JSON → fallback NLUResult
- Failure: JSON in prose still parsed correctly
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock

from src.nlu_processor import NLUProcessor
from src.models import NLUResult, LLMResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONFIG = {
    "preprocessing": {
        "nlu_processor": {
            "model": "claude-haiku-4-5-20251001",
            "confidence_threshold": 0.5,
            "history_turns": 2,
            "intents": [
                "market_truth_query", "scheme_query", "training_query",
                "apply_now", "counsellor_request", "pay_range_query", "unknown",
            ],
            "entities": ["trade", "location", "distance_km", "income_urgency"],
            "sentiment_classes": ["neutral", "positive", "distressed", "frustrated"],
        }
    }
}

NO_HISTORY: list[dict] = []


def make_llm_returning(intent="unknown", entities=None, sentiment="neutral", confidence=0.9) -> MagicMock:
    llm = MagicMock()
    llm.call.return_value = LLMResponse(
        content=json.dumps({
            "intent": intent,
            "entities": entities or {},
            "sentiment": sentiment,
            "confidence": confidence,
        }),
        stop_reason="end_turn",
        model_used="claude-haiku-4-5-20251001",
    )
    return llm


@pytest.fixture
def processor():
    return NLUProcessor()


# ---------------------------------------------------------------------------
# Normal execution — intent classification
# ---------------------------------------------------------------------------


def test_market_truth_query_classified(processor):
    llm = make_llm_returning(intent="market_truth_query", entities={"location": "Hubli"})
    result = processor.process("kaam chahiye Hubli mein", NO_HISTORY, CONFIG, llm)
    assert result.intent == "market_truth_query"
    assert result.entities.get("location") == "Hubli"
    assert result.confidence == 0.9


def test_scheme_query_classified(processor):
    llm = make_llm_returning(intent="scheme_query")
    result = processor.process("PMKVY ke baare mein batao", NO_HISTORY, CONFIG, llm)
    assert result.intent == "scheme_query"


def test_training_query_classified(processor):
    llm = make_llm_returning(intent="training_query", entities={"trade": "electrician"})
    result = processor.process("electrician course kahan hai", NO_HISTORY, CONFIG, llm)
    assert result.intent == "training_query"
    assert result.entities.get("trade") == "electrician"


def test_apply_now_classified(processor):
    llm = make_llm_returning(intent="apply_now")
    result = processor.process("apply kar do", NO_HISTORY, CONFIG, llm)
    assert result.intent == "apply_now"


def test_counsellor_request_classified(processor):
    llm = make_llm_returning(intent="counsellor_request")
    result = processor.process("counsellor chahiye", NO_HISTORY, CONFIG, llm)
    assert result.intent == "counsellor_request"


def test_pay_range_query_classified(processor):
    llm = make_llm_returning(intent="pay_range_query")
    result = processor.process("kitna milega", NO_HISTORY, CONFIG, llm)
    assert result.intent == "pay_range_query"


def test_distress_sentiment_detected(processor):
    llm = make_llm_returning(intent="unknown", sentiment="distressed")
    result = processor.process("bahut mushkil hai", NO_HISTORY, CONFIG, llm)
    assert result.sentiment == "distressed"


def test_multiple_entities_extracted(processor):
    llm = make_llm_returning(
        intent="market_truth_query",
        entities={"trade": "welder", "location": "Dharwad"},
    )
    result = processor.process("welder Dharwad mein kaam chahiye", NO_HISTORY, CONFIG, llm)
    assert result.entities.get("trade") == "welder"
    assert result.entities.get("location") == "Dharwad"


def test_model_override_passed_from_config(processor):
    llm = make_llm_returning(intent="market_truth_query")
    processor.process("kaam chahiye", NO_HISTORY, CONFIG, llm)
    call_kwargs = llm.call.call_args[1]
    assert call_kwargs.get("model_override") == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# History injection — key Agent Core behaviour
# ---------------------------------------------------------------------------


def test_recent_history_injected_into_llm_messages(processor):
    """Agent Core NLU injects history so follow-up intents are resolved correctly."""
    history = [
        {"role": "user", "content": "kaam chahiye Hubli mein"},
        {"role": "assistant", "content": "Hubli mein ITI centres hain."},
    ]
    llm = make_llm_returning(intent="market_truth_query")
    processor.process("aur batao", history, CONFIG, llm)
    call_kwargs = llm.call.call_args[1]
    messages_sent = call_kwargs.get("messages", [])
    # History messages should appear before the current user message
    assert len(messages_sent) >= 3
    assert messages_sent[-1]["content"] == "aur batao"
    assert messages_sent[-1]["role"] == "user"
    # Prior turn content should be present
    contents = [m["content"] for m in messages_sent]
    assert "kaam chahiye Hubli mein" in contents


def test_history_turns_config_limits_injected_history(processor):
    """history_turns=2 means at most 4 messages (2 turns × 2 msgs/turn) from history."""
    long_history = []
    for i in range(10):
        long_history.append({"role": "user", "content": f"turn {i}"})
        long_history.append({"role": "assistant", "content": f"response {i}"})

    llm = make_llm_returning(intent="market_truth_query")
    processor.process("latest message", long_history, CONFIG, llm)
    call_kwargs = llm.call.call_args[1]
    messages_sent = call_kwargs.get("messages", [])
    # 2 history_turns × 2 msgs/turn + 1 current = 5 max
    assert len(messages_sent) <= 5


def test_empty_history_does_not_crash(processor):
    llm = make_llm_returning(intent="market_truth_query")
    result = processor.process("kaam chahiye", [], CONFIG, llm)
    assert result.intent == "market_truth_query"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_input_returns_fallback_without_llm_call(processor):
    llm = MagicMock()
    result = processor.process("", NO_HISTORY, CONFIG, llm)
    assert result.intent == "unknown"
    assert result.confidence == 0.0
    llm.call.assert_not_called()


def test_invalid_intent_from_llm_falls_back_to_unknown(processor):
    llm = make_llm_returning(intent="completely_invalid_intent", confidence=0.95)
    result = processor.process("some message", NO_HISTORY, CONFIG, llm)
    assert result.intent == "unknown"


def test_non_dict_entities_treated_as_empty(processor):
    llm = MagicMock()
    llm.call.return_value = LLMResponse(
        content=json.dumps({
            "intent": "market_truth_query",
            "entities": "not a dict",
            "sentiment": "neutral",
            "confidence": 0.9,
        }),
        stop_reason="end_turn",
        model_used="claude-haiku-4-5-20251001",
    )
    result = processor.process("kaam chahiye", NO_HISTORY, CONFIG, llm)
    assert result.entities == {}


def test_returns_nlu_result_type(processor):
    llm = make_llm_returning(intent="market_truth_query")
    result = processor.process("kaam chahiye", NO_HISTORY, CONFIG, llm)
    assert isinstance(result, NLUResult)


# ---------------------------------------------------------------------------
# Failure scenarios
# ---------------------------------------------------------------------------


def test_llm_error_stop_reason_returns_fallback(processor):
    llm = MagicMock()
    llm.call.return_value = LLMResponse(content=None, stop_reason="error")
    result = processor.process("kaam chahiye", NO_HISTORY, CONFIG, llm)
    assert result.intent == "unknown"
    assert result.confidence == 0.0


def test_llm_exception_returns_fallback_gracefully(processor):
    llm = MagicMock()
    llm.call.side_effect = RuntimeError("network error")
    result = processor.process("kaam chahiye", NO_HISTORY, CONFIG, llm)
    assert result.intent == "unknown"
    assert result.confidence == 0.0


def test_malformed_json_returns_fallback(processor):
    llm = MagicMock()
    llm.call.return_value = LLMResponse(
        content="This is not JSON at all",
        stop_reason="end_turn",
        model_used="claude-haiku-4-5-20251001",
    )
    result = processor.process("kaam chahiye", NO_HISTORY, CONFIG, llm)
    assert result.intent == "unknown"
    assert result.confidence == 0.0


def test_json_in_prose_extracted_and_parsed(processor):
    prose = 'Sure! Here is the result: {"intent": "scheme_query", "entities": {}, "sentiment": "neutral", "confidence": 0.85}'
    llm = MagicMock()
    llm.call.return_value = LLMResponse(
        content=prose, stop_reason="end_turn", model_used="claude-haiku-4-5-20251001"
    )
    result = processor.process("PMKVY batao", NO_HISTORY, CONFIG, llm)
    assert result.intent == "scheme_query"
    assert result.confidence == 0.85


def test_never_raises_on_unexpected_exception(processor):
    """process() must never propagate unexpected exceptions to the caller."""
    llm = MagicMock()
    llm.call.side_effect = Exception("totally unexpected")
    result = processor.process("some input", NO_HISTORY, CONFIG, llm)
    assert isinstance(result, NLUResult)
