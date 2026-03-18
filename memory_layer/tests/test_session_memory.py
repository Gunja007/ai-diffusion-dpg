"""
memory_layer/tests/test_session_memory.py

Unit tests for InProcessSessionMemory.

Covers:
- New session returns empty state
- Write then read returns stored state
- get_user_profile always returns hardcoded PoC demo profile
- History accumulates correctly across writes
- clear_session removes state; next read returns empty
- Concurrent write from two threads does not corrupt state
- None session_id raises ValueError
- Overwrite existing session works correctly
"""

import threading
import pytest

from src.session_memory import InProcessSessionMemory, _POC_DEMO_PROFILE

CONFIG = {"memory": {"session_ttl_seconds": 3600, "max_sessions": 1000}}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def memory():
    return InProcessSessionMemory(config=CONFIG)


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

def test_none_config_raises():
    with pytest.raises(ValueError, match="config must not be None"):
        InProcessSessionMemory(config=None)


# ---------------------------------------------------------------------------
# read_session
# ---------------------------------------------------------------------------

def test_new_session_returns_empty_state(memory):
    state = memory.read_session("sess-001")
    assert state["session_id"] == "sess-001"
    assert state["history"] == []
    assert state["confirmed_entities"] == {}
    assert state["workflow_step"] is None


def test_none_session_id_raises_on_read(memory):
    with pytest.raises(ValueError, match="session_id must not be None"):
        memory.read_session(None)


# ---------------------------------------------------------------------------
# write_session + read_session
# ---------------------------------------------------------------------------

def test_write_then_read_returns_stored_state(memory):
    state = {
        "session_id": "sess-002",
        "history": [
            {"role": "user", "content": "kaam chahiye"},
            {"role": "assistant", "content": "Hubli mein ITI centre hai."},
        ],
        "confirmed_entities": {"trade": "electrician", "location": "Hubli"},
        "workflow_step": None,
        "user_profile": {},
    }
    memory.write_session("sess-002", state)
    result = memory.read_session("sess-002")
    assert result["history"] == state["history"]
    assert result["confirmed_entities"] == {"trade": "electrician", "location": "Hubli"}


def test_overwrite_existing_session(memory):
    memory.write_session("sess-003", {"session_id": "sess-003", "history": ["turn1"], "confirmed_entities": {}, "workflow_step": None, "user_profile": {}})
    memory.write_session("sess-003", {"session_id": "sess-003", "history": ["turn1", "turn2"], "confirmed_entities": {}, "workflow_step": None, "user_profile": {}})
    result = memory.read_session("sess-003")
    assert result["history"] == ["turn1", "turn2"]


def test_history_accumulates_across_writes(memory):
    turn1 = {"session_id": "sess-004", "history": [{"role": "user", "content": "hello"}], "confirmed_entities": {}, "workflow_step": None, "user_profile": {}}
    memory.write_session("sess-004", turn1)

    turn2 = {"session_id": "sess-004", "history": [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Namaste!"},
    ], "confirmed_entities": {}, "workflow_step": None, "user_profile": {}}
    memory.write_session("sess-004", turn2)

    result = memory.read_session("sess-004")
    assert len(result["history"]) == 2


def test_none_state_raises_on_write(memory):
    with pytest.raises(ValueError, match="state must not be None"):
        memory.write_session("sess-x", None)


def test_none_session_id_raises_on_write(memory):
    with pytest.raises(ValueError, match="session_id must not be None"):
        memory.write_session(None, {})


# ---------------------------------------------------------------------------
# get_user_profile — hardcoded PoC demo profile
# ---------------------------------------------------------------------------

def test_get_user_profile_returns_poc_demo_profile(memory):
    profile = memory.get_user_profile("sess-005")
    assert profile["trade"] == "electrician"
    assert profile["location"] == "hubli"
    assert profile["language"] == "hindi"


def test_get_user_profile_same_for_any_session(memory):
    """PoC stub always returns the same demo profile regardless of session."""
    p1 = memory.get_user_profile("sess-a")
    p2 = memory.get_user_profile("sess-b")
    assert p1 == p2 == _POC_DEMO_PROFILE


def test_get_user_profile_returns_copy(memory):
    """Mutating returned profile must not affect subsequent calls."""
    profile = memory.get_user_profile("sess-006")
    profile["trade"] = "welder"
    fresh = memory.get_user_profile("sess-006")
    assert fresh["trade"] == "electrician"


# ---------------------------------------------------------------------------
# clear_session
# ---------------------------------------------------------------------------

def test_clear_session_removes_state(memory):
    memory.write_session("sess-007", {"session_id": "sess-007", "history": ["x"], "confirmed_entities": {}, "workflow_step": None, "user_profile": {}})
    memory.clear_session("sess-007")
    result = memory.read_session("sess-007")
    assert result["history"] == []


def test_clear_nonexistent_session_is_noop(memory):
    """Clearing a session that never existed must not raise."""
    memory.clear_session("sess-nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

def test_concurrent_writes_do_not_corrupt_state(memory):
    """Two threads writing different sessions simultaneously must not corrupt either."""
    errors = []

    def write_many(session_id: str, n: int):
        for i in range(n):
            try:
                memory.write_session(
                    session_id,
                    {"session_id": session_id, "history": list(range(i)), "confirmed_entities": {}, "workflow_step": None, "user_profile": {}},
                )
            except Exception as e:
                errors.append(e)

    t1 = threading.Thread(target=write_many, args=("thread-sess-1", 50))
    t2 = threading.Thread(target=write_many, args=("thread-sess-2", 50))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"Thread errors: {errors}"
    # Both sessions should be independently readable
    s1 = memory.read_session("thread-sess-1")
    s2 = memory.read_session("thread-sess-2")
    assert s1["session_id"] == "thread-sess-1"
    assert s2["session_id"] == "thread-sess-2"
