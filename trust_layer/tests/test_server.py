"""
trust_layer/tests/test_server.py

Unit tests for the Trust Layer FastAPI server (src/server.py).
Uses FastAPI TestClient — no real HTTP calls made.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from server import create_app
from guardrails import BasicTrustLayer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = {
    "trust": {
        "input_rules": {
            "blocked_phrases": ["kill", "bomb"],
            "escalation_topics": ["suicide", "self harm"],
        },
        "output_rules": {
            "blocked_phrases": ["confidential"],
        },
    }
}


@pytest.fixture
def trust():
    return BasicTrustLayer(MINIMAL_CONFIG)


@pytest.fixture
def client(trust):
    app = create_app(trust)
    return TestClient(app)


# ---------------------------------------------------------------------------
# create_app validation
# ---------------------------------------------------------------------------

def test_create_app_none_raises():
    with pytest.raises(ValueError, match="trust must not be None"):
        create_app(None)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /check/input
# ---------------------------------------------------------------------------

def test_check_input_clean_message_returns_allow(client):
    response = client.post("/check/input", json={"session_id": "s1", "message": "kaam chahiye Hubli mein"})
    assert response.status_code == 200
    data = response.json()
    assert data["passed"] is True
    assert data["action"] == "allow"


def test_check_input_blocked_phrase_returns_block(client):
    response = client.post("/check/input", json={"session_id": "s1", "message": "I will kill you"})
    assert response.status_code == 200
    data = response.json()
    assert data["passed"] is False
    assert data["action"] == "block"
    assert data["reason"] is not None


def test_check_input_escalation_topic_returns_escalate(client):
    response = client.post("/check/input", json={"session_id": "s1", "message": "I am thinking about suicide"})
    assert response.status_code == 200
    data = response.json()
    assert data["passed"] is False
    assert data["action"] == "escalate"


def test_check_input_empty_message_returns_allow(client):
    response = client.post("/check/input", json={"session_id": "s1", "message": ""})
    assert response.status_code == 200
    assert response.json()["passed"] is True


def test_check_input_exception_fails_open():
    mock_trust = MagicMock()
    mock_trust.check_input.side_effect = RuntimeError("trust error")
    app = create_app(mock_trust)
    tc = TestClient(app)
    response = tc.post("/check/input", json={"session_id": "s1", "message": "hello"})
    assert response.status_code == 200
    data = response.json()
    assert data["passed"] is True
    assert data["action"] == "allow"


def test_check_input_response_has_required_keys(client):
    response = client.post("/check/input", json={"session_id": "s1", "message": "test"})
    data = response.json()
    assert "passed" in data
    assert "action" in data


# ---------------------------------------------------------------------------
# POST /check/output
# ---------------------------------------------------------------------------

def test_check_output_clean_response_returns_allow(client):
    response = client.post("/check/output", json={"session_id": "s1", "response": "Electrician jobs in Hubli."})
    assert response.status_code == 200
    data = response.json()
    assert data["passed"] is True
    assert data["action"] == "allow"


def test_check_output_blocked_phrase_returns_block(client):
    response = client.post("/check/output", json={"session_id": "s1", "response": "This is confidential data."})
    assert response.status_code == 200
    data = response.json()
    assert data["passed"] is False
    assert data["action"] == "block"


def test_check_output_empty_response_returns_allow(client):
    response = client.post("/check/output", json={"session_id": "s1", "response": ""})
    assert response.status_code == 200
    assert response.json()["passed"] is True


def test_check_output_exception_fails_open():
    mock_trust = MagicMock()
    mock_trust.check_output.side_effect = RuntimeError("output error")
    app = create_app(mock_trust)
    tc = TestClient(app)
    response = tc.post("/check/output", json={"session_id": "s1", "response": "hello"})
    assert response.status_code == 200
    assert response.json()["passed"] is True
    assert response.json()["action"] == "allow"


def test_check_output_reason_none_for_clean_response(client):
    response = client.post("/check/output", json={"session_id": "s2", "response": "Good answer."})
    assert response.json().get("reason") is None


# ---------------------------------------------------------------------------
# POST /check/consent
# ---------------------------------------------------------------------------

def test_check_consent_returns_granted(client):
    response = client.post("/check/consent", json={"session_id": "s1", "connector_name": "job_apply"})
    assert response.status_code == 200
    assert response.json()["granted"] is True


def test_check_consent_exception_fails_open():
    mock_trust = MagicMock()
    mock_trust.check_consent.side_effect = RuntimeError("consent error")
    app = create_app(mock_trust)
    tc = TestClient(app)
    response = tc.post("/check/consent", json={"session_id": "s1", "connector_name": "job_apply"})
    assert response.status_code == 200
    assert response.json()["granted"] is True


def test_check_consent_response_has_granted_key(client):
    response = client.post("/check/consent", json={"session_id": "s1", "connector_name": "any_connector"})
    assert "granted" in response.json()


# ---------------------------------------------------------------------------
# Blocked phrase is case-insensitive
# ---------------------------------------------------------------------------

def test_check_input_block_is_case_insensitive(client):
    response = client.post("/check/input", json={"session_id": "s1", "message": "BOMB THREAT"})
    assert response.json()["action"] == "block"


def test_check_output_block_is_case_insensitive(client):
    response = client.post("/check/output", json={"session_id": "s1", "response": "CONFIDENTIAL info here"})
    assert response.json()["action"] == "block"
