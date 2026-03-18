"""
memory_layer/src/server.py

FastAPI server wrapping InProcessSessionMemory.
Port: 8002

Exposes:
  POST /session/read   — load session state
  POST /session/write  — persist session state
  GET  /profile/{session_id} — get user profile
  DELETE /session/{session_id} — clear session
  GET  /health         — liveness probe
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from session_memory import InProcessSessionMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class SessionReadRequest(BaseModel):
    session_id: str


class SessionWriteRequest(BaseModel):
    session_id: str
    state: dict[str, Any]


class StatusResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(memory: InProcessSessionMemory) -> FastAPI:
    """
    Factory that wires the InProcessSessionMemory instance into the FastAPI app.

    Args:
        memory: Pre-constructed InProcessSessionMemory instance.

    Returns:
        Configured FastAPI application.
    """
    if memory is None:
        raise ValueError("memory must not be None")

    app = FastAPI(
        title="Memory Layer Service",
        description="Session state management service for the DPG AI framework.",
        version="0.1.0",
    )

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    @app.post("/session/read")
    def session_read(request: SessionReadRequest) -> dict:
        """Load session state for the given session_id."""
        start = time.time()
        session_id = request.session_id

        if not session_id:
            return _empty_state(session_id)

        try:
            state = memory.read_session(session_id)
            logger.info(
                "memory_server.session_read",
                extra={
                    "operation": "server.session_read",
                    "status": "success",
                    "session_id": session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return state if isinstance(state, dict) else _state_to_dict(state, session_id)

        except Exception as e:
            logger.error(
                "memory_server.session_read_error",
                extra={
                    "operation": "server.session_read",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return _empty_state(session_id)

    @app.post("/session/write")
    def session_write(request: SessionWriteRequest) -> StatusResponse:
        """Persist session state for the given session_id."""
        start = time.time()
        session_id = request.session_id

        if not session_id:
            return StatusResponse(status="ok")

        try:
            memory.write_session(session_id, request.state)
            logger.info(
                "memory_server.session_write",
                extra={
                    "operation": "server.session_write",
                    "status": "success",
                    "session_id": session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
        except Exception as e:
            logger.error(
                "memory_server.session_write_error",
                extra={
                    "operation": "server.session_write",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        return StatusResponse(status="ok")

    @app.get("/profile/{session_id}")
    def get_profile(session_id: str) -> dict:
        """Return persistent user profile for the given session_id."""
        start = time.time()

        if not session_id:
            return {}

        try:
            profile = memory.get_user_profile(session_id)
            logger.info(
                "memory_server.get_profile",
                extra={
                    "operation": "server.get_profile",
                    "status": "success",
                    "session_id": session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return profile if isinstance(profile, dict) else {}

        except Exception as e:
            logger.error(
                "memory_server.get_profile_error",
                extra={
                    "operation": "server.get_profile",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return {}

    @app.delete("/session/{session_id}")
    def clear_session(session_id: str) -> StatusResponse:
        """Delete all session-scoped state for the given session_id."""
        start = time.time()

        if not session_id:
            return StatusResponse(status="ok")

        try:
            memory.clear_session(session_id)
            logger.info(
                "memory_server.clear_session",
                extra={
                    "operation": "server.clear_session",
                    "status": "success",
                    "session_id": session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
        except Exception as e:
            logger.error(
                "memory_server.clear_session_error",
                extra={
                    "operation": "server.clear_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        return StatusResponse(status="ok")

    @app.get("/health")
    def health() -> StatusResponse:
        """Liveness probe."""
        return StatusResponse(status="ok")

    return app


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _empty_state(session_id: str) -> dict:
    """Return a blank session dict."""
    return {
        "session_id": session_id,
        "history": [],
        "confirmed_entities": {},
        "workflow_step": None,
        "user_profile": {},
    }


def _state_to_dict(state: Any, session_id: str) -> dict:
    """Convert a SessionState dataclass to a plain dict if needed."""
    try:
        return {
            "session_id": getattr(state, "session_id", session_id),
            "history": getattr(state, "history", []),
            "confirmed_entities": getattr(state, "confirmed_entities", {}),
            "workflow_step": getattr(state, "workflow_step", None),
            "user_profile": getattr(state, "user_profile", {}),
        }
    except Exception:
        return _empty_state(session_id)
