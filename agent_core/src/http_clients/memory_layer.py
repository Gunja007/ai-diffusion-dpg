"""
agent_core/src/memory_http_client.py

HTTP client for the Memory Layer service at port 8002.
Implements the same interface as MemoryLayerBase.

Config reads from:
  memory_client.endpoint   (default "http://localhost:8002")
  memory_client.timeout_ms (default 3000)

Error handling:
  - read_session: returns SessionState.empty(session_id) on any failure.
  - write_session / get_user_profile / clear_session: log and continue.
  - Never raises to the caller.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.interfaces.memory_layer import MemoryLayerBase
from src.models import SessionState

logger = logging.getLogger(__name__)


class MemoryLayerHttpClient(MemoryLayerBase):
    """
    HTTP client that calls the Memory Layer service.

    Implements the MemoryLayerBase interface contract so it can be swapped
    with any other implementation without changing the orchestrator.

    Args:
        config: Full config dict. Reads memory_client.endpoint and
                memory_client.timeout_ms.
    """

    def __init__(self, config: dict) -> None:
        if config is None:
            raise ValueError("config must not be None")

        client_cfg = config.get("memory_client", {})
        self._endpoint: str = client_cfg.get("endpoint", "http://localhost:8002")
        self._timeout_s: float = client_cfg.get("timeout_ms", 3000) / 1000

        logger.info(
            "memory_http_client.init",
            extra={
                "operation": "memory_http_client.init",
                "status": "success",
                "endpoint": self._endpoint,
                "timeout_s": self._timeout_s,
            },
        )

    # ------------------------------------------------------------------
    # Public interface — mirrors MemoryLayerBase
    # ------------------------------------------------------------------

    def read_session(self, session_id: str) -> SessionState:
        """
        Load session state for the given session.
        Returns SessionState.empty(session_id) on any failure.
        Never raises.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")

        start = time.time()

        try:
            response = httpx.post(
                f"{self._endpoint}/session/read",
                json={"session_id": session_id},
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            data = response.json()

            state = SessionState(
                session_id=data.get("session_id", session_id),
                history=data.get("history", []),
                confirmed_entities=data.get("confirmed_entities", {}),
                workflow_step=data.get("workflow_step"),
                user_profile=data.get("user_profile", {}),
            )

            logger.info(
                "memory_http_client.read_session",
                extra={
                    "operation": "memory_http_client.read_session",
                    "status": "success",
                    "session_id": session_id,
                    "history_turns": len(state.history) // 2,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return state

        except httpx.TimeoutException as e:
            logger.error(
                "memory_http_client.read_session_timeout",
                extra={
                    "operation": "memory_http_client.read_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"TimeoutException: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return SessionState.empty(session_id)

        except httpx.HTTPStatusError as e:
            logger.error(
                "memory_http_client.read_session_http_error",
                extra={
                    "operation": "memory_http_client.read_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return SessionState.empty(session_id)

        except Exception as e:
            logger.error(
                "memory_http_client.read_session_error",
                extra={
                    "operation": "memory_http_client.read_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return SessionState.empty(session_id)

    def write_session(self, session_id: str, state: SessionState) -> None:
        """
        Persist session state. Called asynchronously — logs and continues on failure.
        Never raises.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")
        if state is None:
            raise ValueError("state must not be None")

        start = time.time()

        state_dict = _session_state_to_dict(state, session_id)

        try:
            response = httpx.post(
                f"{self._endpoint}/session/write",
                json={"session_id": session_id, "state": state_dict},
                timeout=self._timeout_s,
            )
            response.raise_for_status()

            logger.info(
                "memory_http_client.write_session",
                extra={
                    "operation": "memory_http_client.write_session",
                    "status": "success",
                    "session_id": session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        except httpx.TimeoutException as e:
            logger.error(
                "memory_http_client.write_session_timeout",
                extra={
                    "operation": "memory_http_client.write_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"TimeoutException: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        except httpx.HTTPStatusError as e:
            logger.error(
                "memory_http_client.write_session_http_error",
                extra={
                    "operation": "memory_http_client.write_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        except Exception as e:
            logger.error(
                "memory_http_client.write_session_error",
                extra={
                    "operation": "memory_http_client.write_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

    def get_user_profile(self, session_id: str) -> dict:
        """
        Return persistent user profile data.
        Returns an empty dict on any failure.
        Never raises.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")

        start = time.time()

        try:
            response = httpx.get(
                f"{self._endpoint}/profile/{session_id}",
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            profile = response.json()

            logger.info(
                "memory_http_client.get_user_profile",
                extra={
                    "operation": "memory_http_client.get_user_profile",
                    "status": "success",
                    "session_id": session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return profile if isinstance(profile, dict) else {}

        except httpx.TimeoutException as e:
            logger.error(
                "memory_http_client.get_user_profile_timeout",
                extra={
                    "operation": "memory_http_client.get_user_profile",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"TimeoutException: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return {}

        except httpx.HTTPStatusError as e:
            logger.error(
                "memory_http_client.get_user_profile_http_error",
                extra={
                    "operation": "memory_http_client.get_user_profile",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return {}

        except Exception as e:
            logger.error(
                "memory_http_client.get_user_profile_error",
                extra={
                    "operation": "memory_http_client.get_user_profile",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return {}

    def clear_session(self, session_id: str) -> None:
        """
        Delete all session-scoped state for the given session_id.
        Logs and continues on failure. Never raises.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")

        start = time.time()

        try:
            response = httpx.delete(
                f"{self._endpoint}/session/{session_id}",
                timeout=self._timeout_s,
            )
            response.raise_for_status()

            logger.info(
                "memory_http_client.clear_session",
                extra={
                    "operation": "memory_http_client.clear_session",
                    "status": "success",
                    "session_id": session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        except httpx.TimeoutException as e:
            logger.error(
                "memory_http_client.clear_session_timeout",
                extra={
                    "operation": "memory_http_client.clear_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"TimeoutException: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        except httpx.HTTPStatusError as e:
            logger.error(
                "memory_http_client.clear_session_http_error",
                extra={
                    "operation": "memory_http_client.clear_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        except Exception as e:
            logger.error(
                "memory_http_client.clear_session_error",
                extra={
                    "operation": "memory_http_client.clear_session",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _session_state_to_dict(state: Any, session_id: str) -> dict:
    """Convert a SessionState dataclass to a plain dict for JSON serialisation."""
    if isinstance(state, dict):
        return state
    try:
        return {
            "session_id": getattr(state, "session_id", session_id),
            "history": getattr(state, "history", []),
            "confirmed_entities": getattr(state, "confirmed_entities", {}),
            "workflow_step": getattr(state, "workflow_step", None),
            "user_profile": getattr(state, "user_profile", {}),
        }
    except Exception:
        return {
            "session_id": session_id,
            "history": [],
            "confirmed_entities": {},
            "workflow_step": None,
            "user_profile": {},
        }
