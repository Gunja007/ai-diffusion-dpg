"""
agent_core/tests/test_language_normalisation.py

Unit tests for LanguageNormaliser (agent_core/src/language_normalisation.py).
All LLM calls are mocked — no real API calls.

Coverage:
- Normal: hindi/hinglish/english/kannada detected and normalised text returned
- Normal: model_override passed from config to llm.call
- Normal: JSON embedded in prose is still parsed correctly
- Edge: empty input returns (raw_input, "") without calling LLM
- Edge: missing config section uses defaults
- Failure: bhashini provider raises NotImplementedError
- Failure: LLM returns error stop_reason → falls back to (raw_input, "")
- Failure: LLM raises exception → falls back to (raw_input, "")
- Failure: malformed JSON → falls back to (raw_input, "")
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock

from src.language_normalisation import LanguageNormaliser
from src.models import LLMResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONFIG = {
    "preprocessing": {
        "language_normalisation": {
            "model": "claude-haiku-4-5-20251001",
            "provider": "llm_native",
            "supported_languages": ["hindi", "kannada", "english", "hinglish"],
            "transliteration": True,
            "code_switching": True,
        }
    }
}

CONFIG_NO_PREPROCESSING = {}


def make_llm_returning(normalised: str, detected: str) -> MagicMock:
    llm = MagicMock()
    llm.call.return_value = LLMResponse(
        content=json.dumps({
            "detected_language": detected,
            "normalised_text": normalised,
        }),
        stop_reason="end_turn",
        model_used="claude-haiku-4-5-20251001",
    )
    return llm


@pytest.fixture
def normaliser():
    return LanguageNormaliser()


# ---------------------------------------------------------------------------
# Normal execution
# ---------------------------------------------------------------------------


def test_hindi_input_detected_and_returned(normaliser):
    llm = make_llm_returning("kaam chahiye", "hindi")
    normalised, detected = normaliser.normalise("kaam chahiye", CONFIG, llm)
    assert detected == "hindi"
    assert normalised == "kaam chahiye"


def test_hinglish_input_detected(normaliser):
    llm = make_llm_returning("electrician ka kaam chahiye", "hinglish")
    normalised, detected = normaliser.normalise("electrician ka kaam chahiye", CONFIG, llm)
    assert detected == "hinglish"


def test_english_input_detected(normaliser):
    llm = make_llm_returning("I want electrician work", "english")
    _, detected = normaliser.normalise("I want electrician work", CONFIG, llm)
    assert detected == "english"


def test_kannada_input_detected(normaliser):
    llm = make_llm_returning("ನಾನು ವಿದ್ಯುತ್ ತಂತ್ರಜ್ಞ", "kannada")
    _, detected = normaliser.normalise("ನಾನು ವಿದ್ಯುತ್ ತಂತ್ರಜ್ಞ", CONFIG, llm)
    assert detected == "kannada"


def test_normalised_text_from_llm_returned(normaliser):
    llm = make_llm_returning("cleaned and normalised text", "english")
    normalised, _ = normaliser.normalise("original input", CONFIG, llm)
    assert normalised == "cleaned and normalised text"


def test_model_override_passed_from_config(normaliser):
    llm = make_llm_returning("hello", "english")
    normaliser.normalise("hello", CONFIG, llm)
    call_kwargs = llm.call.call_args[1]
    assert call_kwargs.get("model_override") == "claude-haiku-4-5-20251001"


def test_json_embedded_in_prose_parsed(normaliser):
    """LLM sometimes wraps JSON in prose — must still parse correctly."""
    prose = 'Here is the result: {"detected_language": "hinglish", "normalised_text": "kaam chahiye"}'
    llm = MagicMock()
    llm.call.return_value = LLMResponse(
        content=prose, stop_reason="end_turn", model_used="claude-haiku-4-5-20251001"
    )
    normalised, detected = normaliser.normalise("kaam chahiye", CONFIG, llm)
    assert detected == "hinglish"
    assert normalised == "kaam chahiye"


def test_missing_preprocessing_config_uses_defaults(normaliser):
    """No preprocessing section → uses supported_languages default, no model_override."""
    llm = make_llm_returning("hello", "english")
    normalised, detected = normaliser.normalise("hello", CONFIG_NO_PREPROCESSING, llm)
    assert detected == "english"
    assert normalised == "hello"
    call_kwargs = llm.call.call_args[1]
    assert call_kwargs.get("model_override") is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_input_returns_raw_and_empty_language_without_llm_call(normaliser):
    llm = MagicMock()
    normalised, detected = normaliser.normalise("", CONFIG, llm)
    assert normalised == ""
    assert detected == ""
    llm.call.assert_not_called()


# ---------------------------------------------------------------------------
# Bhashini provider stub
# ---------------------------------------------------------------------------


def test_bhashini_provider_raises_not_implemented(normaliser):
    bhashini_config = {
        "preprocessing": {
            "language_normalisation": {
                "provider": "bhashini",
                "supported_languages": ["hindi"],
            }
        }
    }
    llm = MagicMock()
    with pytest.raises(NotImplementedError, match="Bhashini provider"):
        normaliser.normalise("kaam chahiye", bhashini_config, llm)


# ---------------------------------------------------------------------------
# Failure scenarios
# ---------------------------------------------------------------------------


def test_llm_error_stop_reason_falls_back_to_raw_input(normaliser):
    llm = MagicMock()
    llm.call.return_value = LLMResponse(content=None, stop_reason="error")
    original = "kaam chahiye Hubli mein"
    normalised, detected = normaliser.normalise(original, CONFIG, llm)
    assert normalised == original
    assert detected == ""


def test_llm_exception_falls_back_gracefully(normaliser):
    llm = MagicMock()
    llm.call.side_effect = RuntimeError("network error")
    original = "kaam chahiye"
    normalised, detected = normaliser.normalise(original, CONFIG, llm)
    assert normalised == original
    assert detected == ""


def test_malformed_json_falls_back_to_raw_input(normaliser):
    llm = MagicMock()
    llm.call.return_value = LLMResponse(
        content="not valid json here",
        stop_reason="end_turn",
        model_used="claude-haiku-4-5-20251001",
    )
    original = "kaam chahiye"
    normalised, detected = normaliser.normalise(original, CONFIG, llm)
    assert normalised == original
    assert detected == ""


def test_never_raises_on_unexpected_exception(normaliser):
    """normalise() must never propagate unexpected exceptions to the caller."""
    llm = MagicMock()
    llm.call.side_effect = Exception("totally unexpected")
    result = normaliser.normalise("some input", CONFIG, llm)
    assert isinstance(result, tuple)
    assert len(result) == 2
