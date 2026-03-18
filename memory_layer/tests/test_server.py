"""
memory_layer/tests/test_server.py

Unit tests for the Memory Layer FastAPI server (src/server.py).
Uses FastAPI TestClient — no real HTTP calls made.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from server import create_app
from session_memory import InProcessSessionMemory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def memory():
    return InProcessSessionMemory(config={})


@pytest.fixture
def client(memory):
    app = create_app(memory)
    return TestClient(app)


# ---------------------------------------------------------------------------
# create_app validation
# ---------------------------------------------------------------------------

def test_create_app_none_raises():
    with pytest.raises(ValueError, match="memory must not be None"):
        create_app(None)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /session/read
# ---------------------------------------------------------------------------

def test_session_read_new_session_returns_empty_state(client):
    response = client.post("/session/read", json={"session_id": "sess-new"})
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "sess-new"
    assert data["history"] == []
    assert data["confirmed_entities"] == {}


def test_session_read_after_write_returns_state(client, memory):
    state = {
        "session_id": "sess-1",
        "history": [{"role": "user", "content": "hello"}],
        "confirmed_entities": {"trade": "electrician"},
        "workflow_step": None,
        "user_profile": {},
    }
    memory.write_session("sess-1", state)
    response = client.post("/session/read", json={"session_id": "sess-1"})
    assert response.status_code == 200
    data = response.json()
    assert data["confirmed_entities"]["trade"] == "electrician"


def test_session_read_empty_session_id_returns_empty(client):
    response = client.post("/session/read", json={"session_id": ""})
    assert response.status_code == 200
    data = response.json()
    assert data["history"] == []


def test_session_read_memory_exception_returns_empty(client):
    mock_memory = MagicMock()
    mock_memory.read_session.side_effect = RuntimeError("db error")
    app = create_app(mock_memory)
    tc = TestClient(app)
    response = tc.post("/session/read", json={"session_id": "sess-err"})
    assert response.status_code == 200
    assert response.json()["history"] == []


# ---------------------------------------------------------------------------
# POST /session/write
# ---------------------------------------------------------------------------

def test_session_write_returns_ok(client):
    state = {"session_id": "sess-2", "history": [], "confirmed_entities": {}, "workflow_step": None, "user_profile": {}}
    response = client.post("/session/write", json={"session_id": "sess-2", "state": state})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_session_write_persists_and_readable(client, memory):
    state = {
        "session_id": "sess-3",
        "history": [{"role": "user", "content": "test"}],
        "confirmed_entities": {},
        "workflow_step": "step1",
        "user_profile": {},
    }
    client.post("/session/write", json={"session_id": "sess-3", "state": state})
    read_response = client.post("/session/read", json={"session_id": "sess-3"})
    assert read_response.json()["workflow_step"] == "step1"


def test_session_write_empty_session_id_returns_ok(client):
    response = client.post("/session/write", json={"session_id": "", "state": {}})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_session_write_memory_exception_still_returns_ok(client):
    mock_memory = MagicMock()
    mock_memory.write_session.side_effect = RuntimeError("write error")
    app = create_app(mock_memory)
    tc = TestClient(app)
    response = tc.post("/session/write", json={"session_id": "sess-err", "state": {}})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /profile/{session_id}
# ---------------------------------------------------------------------------

def test_get_profile_returns_profile(client, memory):
    memory.write_session("sess-4", {
        "session_id": "sess-4", "history": [], "confirmed_entities": {},
        "workflow_step": None, "user_profile": {"trade": "welder"},
    })
    response = client.get("/profile/sess-4")
    assert response.status_code == 200


def test_get_profile_new_session_returns_dict(client):
    response = client.get("/profile/unknown-sess")
    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_get_profile_memory_exception_returns_empty(client):
    mock_memory = MagicMock()
    mock_memory.get_user_profile.side_effect = RuntimeError("profile error")
    app = create_app(mock_memory)
    tc = TestClient(app)
    response = tc.get("/profile/sess-err")
    assert response.status_code == 200
    assert response.json() == {}


# ---------------------------------------------------------------------------
# DELETE /session/{session_id}
# ---------------------------------------------------------------------------

def test_clear_session_returns_ok(client, memory):
    state = {"session_id": "sess-5", "history": [{"role": "user", "content": "x"}],
             "confirmed_entities": {}, "workflow_step": None, "user_profile": {}}
    memory.write_session("sess-5", state)
    response = client.delete("/session/sess-5")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_clear_session_clears_state(client, memory):
    state = {"session_id": "sess-6", "history": [{"role": "user", "content": "hello"}],
             "confirmed_entities": {}, "workflow_step": None, "user_profile": {}}
    memory.write_session("sess-6", state)
    client.delete("/session/sess-6")
    read = client.post("/session/read", json={"session_id": "sess-6"})
    assert read.json()["history"] == []


def test_clear_session_memory_exception_still_returns_ok(client):
    mock_memory = MagicMock()
    mock_memory.clear_session.side_effect = RuntimeError("clear error")
    app = create_app(mock_memory)
    tc = TestClient(app)
    response = tc.delete("/session/sess-err")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
