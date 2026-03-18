"""
tests/test_engine.py

Unit tests for KnowledgeEngine orchestrator (src/engine.py).

Tests cover:
- Full block chain: context flows through all 3 blocks in order
- Prompt assembly: correct message structure with persona, history, RAG chunks
- Empty input: returns [] without running blocks
- Block failure: one block fails → chain continues, result still returned
- NLU params passed in: assemble_prompt accepts pre-computed normalised_input, intent, entities
- End-to-end KKB scenario: "ITI electrician Hubli mein kaam chahiye"
"""

import pytest
from unittest.mock import MagicMock, patch

from src.engine import KnowledgeEngine
from src.base import KEContext, LLMWrapperBase, KnowledgeBlock
from src.models import SessionState, LLMResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8001},
    "knowledge": {
        "conversation": {"max_history_turns": 5},
        "blocks": {
            "glossary": {
                "enabled": True,
                "mappings": [
                    {"colloquial": ["kaam chahiye", "naukri chahiye"], "canonical": "market_truth_query"},
                    {"colloquial": ["electrician", "bijli wala"], "canonical": "trade:electrician"},
                    {"colloquial": ["Hubli", "Dharwad"], "canonical": "location:karnataka_north"},
                ],
                "apply_to": ["normalised_input", "entities"],
            },
            "static_knowledge_base": {
                "enabled": True,
                "chroma_persist_dir": "/tmp/test_chroma_engine",
                "collection_name": "kkb_test",
                "embedding_provider": "sentence_transformers",
                "embedding_model": "paraphrase-multilingual-MiniLM-L12-v2",
                "top_k": 3,
                "similarity_threshold": 0.65,
                "sources": [],
                "metadata_filters": {"use_intent_filter": True, "use_location_filter": True},
            },
            "multimodal_input_handler": {
                "enabled": False,
                "supported_types": ["pdf", "image"],
                "audio_enabled": False,
                "image_model": "claude-sonnet-4-6",
                "max_file_size_mb": 10,
            },
        },
    },
    "conversation": {
        "persona": {
            "text": "You are Kaam Ki Baat, a labour advisory helpline assistant."
        },
        "guardrail_reminders": [
            "Do not discuss topics unrelated to employment or schemes."
        ],
    },
}


def make_session_state(history: list = None) -> SessionState:
    return SessionState(
        session_id="test-session",
        history=history or [],
        confirmed_entities={},
        workflow_step=None,
        user_profile={},
    )


@pytest.fixture
def engine_with_mock_kb():
    """Engine with Static KB block mocked to avoid ChromaDB dependency."""
    engine = KnowledgeEngine(config=CONFIG)

    for i, block in enumerate(engine._blocks):
        if type(block).__name__ == "StaticKnowledgeBaseBlock":
            mock_kb = MagicMock()

            def mock_process(context, llm, cfg):
                context.retrieval_chunks = [
                    {"text": "Electrician jobs available in Hubli district ITI", "metadata": {"doc_type": "trade"}},
                ]
                context.always_include_chunks = [
                    {"text": "Market truth: ONEST data shows X jobs", "metadata": {"doc_type": "always_include"}},
                ]
                return context

            mock_kb.process.side_effect = mock_process
            engine._blocks[i] = mock_kb
            break

    return engine


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_init_none_config_raises():
    with pytest.raises(ValueError, match="config must not be None"):
        KnowledgeEngine(config=None)


def test_init_creates_three_blocks():
    """KE now runs only 3 blocks: Glossary, Static KB, Multimodal."""
    engine = KnowledgeEngine(config=CONFIG)
    assert len(engine._blocks) == 3


def test_init_llm_optional():
    """llm is optional — not needed when multimodal is disabled."""
    engine = KnowledgeEngine(config=CONFIG)
    assert engine._llm is None


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


def test_empty_user_message_returns_empty_list():
    engine = KnowledgeEngine(config=CONFIG)
    result = engine.assemble_prompt(
        session_id="sess1",
        user_message="",
        session_state=make_session_state(),
    )
    assert result == []


def test_whitespace_only_message_is_processed():
    """Whitespace is truthy — engine processes it and returns messages."""
    engine = KnowledgeEngine(config=CONFIG)
    result = engine.assemble_prompt(
        session_id="sess1",
        user_message="   ",
        session_state=make_session_state(),
    )
    assert isinstance(result, list)


def test_none_session_id_raises():
    engine = KnowledgeEngine(config=CONFIG)
    with pytest.raises(ValueError, match="session_id must not be None"):
        engine.assemble_prompt(
            session_id=None,
            user_message="hello",
            session_state=make_session_state(),
        )


# ---------------------------------------------------------------------------
# NLU params passed in (Language Norm and NLU now run in Agent Core)
# ---------------------------------------------------------------------------


def test_nlu_params_pre_populate_context(engine_with_mock_kb):
    """NLU results from Agent Core should appear in the assembled messages."""
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="kaam chahiye Hubli mein",
        session_state=make_session_state(),
        normalised_input="kaam chahiye Hubli mein",
        detected_language="hinglish",
        intent="market_truth_query",
        entities={"trade": "electrician", "location": "Hubli"},
        sentiment="neutral",
        confidence=0.92,
    )
    assert isinstance(result, list)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# Message structure
# ---------------------------------------------------------------------------


def test_assemble_prompt_returns_list_of_dicts(engine_with_mock_kb):
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="kaam chahiye",
        session_state=make_session_state(),
    )
    assert isinstance(result, list)
    assert all(isinstance(m, dict) for m in result)


def test_messages_have_role_and_content_keys(engine_with_mock_kb):
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="kaam chahiye",
        session_state=make_session_state(),
    )
    for msg in result:
        assert "role" in msg
        assert "content" in msg


def test_last_message_is_user_input(engine_with_mock_kb):
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="electrician kaam chahiye Hubli mein",
        session_state=make_session_state(),
    )
    assert result[-1]["role"] == "user"
    assert result[-1]["content"] == "electrician kaam chahiye Hubli mein"


def test_persona_text_in_first_message(engine_with_mock_kb):
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="hello",
        session_state=make_session_state(),
    )
    first_content = result[0]["content"]
    assert "Kaam Ki Baat" in first_content


def test_rag_chunks_in_messages(engine_with_mock_kb):
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="electrician kaam",
        session_state=make_session_state(),
    )
    combined = " ".join(m["content"] for m in result)
    assert "Electrician jobs available" in combined


def test_always_include_chunks_in_messages(engine_with_mock_kb):
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="kaam chahiye",
        session_state=make_session_state(),
    )
    combined = " ".join(m["content"] for m in result)
    assert "Market truth" in combined


def test_history_injected_into_messages(engine_with_mock_kb):
    history = [
        {"role": "user", "content": "kaam chahiye"},
        {"role": "assistant", "content": "Hubli mein ITI centres hain."},
    ]
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="aur batao",
        session_state=make_session_state(history=history),
    )
    contents = [m["content"] for m in result]
    assert "kaam chahiye" in contents
    assert "Hubli mein ITI centres hain." in contents


def test_message_roles_alternate(engine_with_mock_kb):
    """Messages must alternate user/assistant (Anthropic API requirement)."""
    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-session",
        user_message="hello",
        session_state=make_session_state(),
    )
    for i in range(len(result) - 1):
        current_role = result[i]["role"]
        next_role = result[i + 1]["role"]
        assert current_role != next_role, (
            f"Consecutive messages at positions {i} and {i+1} both have role '{current_role}'"
        )


# ---------------------------------------------------------------------------
# Block failure resilience
# ---------------------------------------------------------------------------


def test_block_exception_does_not_crash_engine():
    """If one block raises unexpectedly, the engine should continue and still return messages."""
    engine = KnowledgeEngine(config=CONFIG)

    # Make Block 0 (Glossary) raise
    engine._blocks[0].process = MagicMock(side_effect=RuntimeError("Glossary crashed"))

    # Mock Static KB block to avoid ChromaDB
    for i, block in enumerate(engine._blocks):
        if type(block).__name__ == "StaticKnowledgeBaseBlock":
            mock_kb = MagicMock()
            mock_kb.process.side_effect = lambda ctx, l, c: ctx
            engine._blocks[i] = mock_kb
            break

    result = engine.assemble_prompt(
        session_id="test-session",
        user_message="kaam chahiye",
        session_state=make_session_state(),
    )

    assert isinstance(result, list)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# End-to-end KKB scenario (mocked blocks)
# ---------------------------------------------------------------------------


def test_kkb_e2e_scenario(engine_with_mock_kb):
    """
    Full KKB test: "ITI electrician Hubli mein kaam chahiye"

    Asserts:
    - messages list is non-empty
    - correct role/content structure
    - user input appears in last message
    - persona and context appear in first message
    """
    user_input = "ITI electrician Hubli mein kaam chahiye"
    result = engine_with_mock_kb.assemble_prompt(
        session_id="kkb-e2e",
        user_message=user_input,
        session_state=make_session_state(),
        intent="market_truth_query",
        entities={"trade": "electrician", "location": "Hubli"},
        normalised_input=user_input,
        detected_language="hinglish",
        confidence=0.92,
    )

    assert len(result) >= 3  # at minimum: system context, assistant ack, user message
    assert result[-1]["role"] == "user"
    assert result[-1]["content"] == user_input

    first_content = result[0]["content"]
    assert "Kaam Ki Baat" in first_content  # persona
    assert "Market truth" in " ".join(m["content"] for m in result)  # always_include


def test_history_trimmed_to_max_turns(engine_with_mock_kb):
    """History beyond max_history_turns should be trimmed."""
    long_history = []
    for i in range(20):
        long_history.append({"role": "user", "content": f"turn {i}"})
        long_history.append({"role": "assistant", "content": f"response {i}"})

    result = engine_with_mock_kb.assemble_prompt(
        session_id="test-trim",
        user_message="kaam chahiye",
        session_state=make_session_state(history=long_history),
    )

    # Max 5 turns = 10 messages from history
    history_in_result = [
        m for m in result
        if m["content"].startswith("turn ") or m["content"].startswith("response ")
    ]
    assert len(history_in_result) <= 10
