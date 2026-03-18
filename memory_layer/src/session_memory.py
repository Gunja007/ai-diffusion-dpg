"""
memory_layer/src/session_memory.py

InProcessSessionMemory — PoC stub for the Memory Layer DPG.

Implements the MemoryLayerBase interface using a plain in-process Python dict.
State is lost when the process exits — this is intentional for PoC.

Design notes:
- Thread-safe: write_session() is called from a daemon thread in orchestrator._post_turn().
  A threading.Lock guards all dict mutations.
- get_user_profile() returns a hardcoded KKB demo profile for PoC. In production this
  reads from a persistent user profile store keyed by phone number / user ID.
- session_ttl_seconds in config is documented for future real implementations;
  this stub holds sessions for the lifetime of the process.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PoC demo profile — hardcoded as per Task 2.4 in KKB PoC plan
# ---------------------------------------------------------------------------
_POC_DEMO_PROFILE: dict[str, Any] = {
    "trade": "electrician",
    "location": "hubli",
    "language": "hindi",
}


class InProcessSessionMemory:
    """
    In-process session store. Implements MemoryLayerBase contract.

    All session state lives in a single dict keyed by session_id.
    Thread-safe for concurrent reads and writes via a reentrant lock.

    Args:
        config: Full config dict. Reads memory.session_ttl_seconds and
                memory.max_sessions (both informational in PoC — not enforced).
    """

    def __init__(self, config: dict) -> None:
        if config is None:
            raise ValueError("config must not be None")

        self._config = config
        self._store: dict[str, dict] = {}
        self._lock = threading.Lock()

        memory_cfg = config.get("memory", {})
        logger.info(
            "memory_layer.init",
            extra={
                "operation": "session_memory.init",
                "status": "success",
                "session_ttl_seconds": memory_cfg.get("session_ttl_seconds", 3600),
                "note": "TTL not enforced in PoC stub",
            },
        )

    # ------------------------------------------------------------------
    # Public interface — mirrors MemoryLayerBase
    # ------------------------------------------------------------------

    def read_session(self, session_id: str) -> dict:
        """
        Load session state for the given session.
        Returns an empty session dict if no prior state exists.
        Never raises for a missing session.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")

        with self._lock:
            state = self._store.get(session_id)

        if state is None:
            logger.info(
                "memory_layer.read_session",
                extra={
                    "operation": "session_memory.read_session",
                    "status": "skipped",
                    "session_id": session_id,
                    "note": "new session — returning empty state",
                },
            )
            return _empty_session(session_id)

        logger.info(
            "memory_layer.read_session",
            extra={
                "operation": "session_memory.read_session",
                "status": "success",
                "session_id": session_id,
                "history_turns": len(state.get("history", [])) // 2,
            },
        )
        return state

    def write_session(self, session_id: str, state: dict) -> None:
        """
        Persist session state. Overwrites any existing state for the session.
        Called asynchronously after the response is delivered.
        Thread-safe.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")
        if state is None:
            raise ValueError("state must not be None")

        with self._lock:
            self._store[session_id] = state

        logger.info(
            "memory_layer.write_session",
            extra={
                "operation": "session_memory.write_session",
                "status": "success",
                "session_id": session_id,
                "history_turns": len(state.get("history", [])) // 2,
            },
        )

    def get_user_profile(self, session_id: str) -> dict[str, Any]:
        """
        Return persistent user profile data.

        PoC stub: always returns the hardcoded KKB demo profile
        {trade: electrician, location: hubli, language: hindi}.
        In production, this reads from a persistent store keyed by user ID / phone number.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")

        logger.info(
            "memory_layer.get_user_profile",
            extra={
                "operation": "session_memory.get_user_profile",
                "status": "success",
                "session_id": session_id,
                "note": "returning hardcoded PoC demo profile",
            },
        )
        return dict(_POC_DEMO_PROFILE)

    def write_user_profile(self, session_id: str, profile: dict[str, Any]) -> None:
        """
        Update user profile data.

        PoC stub: stores in-process only — no persistence across restarts.
        In production, this writes to a persistent user profile store.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")
        if profile is None:
            raise ValueError("profile must not be None")

        # In PoC, store alongside session state under a reserved key
        with self._lock:
            if session_id not in self._store:
                self._store[session_id] = _empty_session(session_id)
            self._store[session_id]["_user_profile"] = dict(profile)

        logger.info(
            "memory_layer.write_user_profile",
            extra={
                "operation": "session_memory.write_user_profile",
                "status": "success",
                "session_id": session_id,
                "note": "no persistence in PoC stub",
            },
        )

    def clear_session(self, session_id: str) -> None:
        """Delete all session-scoped state for the given session_id."""
        if session_id is None:
            raise ValueError("session_id must not be None")

        with self._lock:
            removed = self._store.pop(session_id, None)

        logger.info(
            "memory_layer.clear_session",
            extra={
                "operation": "session_memory.clear_session",
                "status": "success" if removed is not None else "skipped",
                "session_id": session_id,
            },
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _empty_session(session_id: str) -> dict:
    """Return a blank session dict for a brand-new session."""
    return {
        "session_id": session_id,
        "history": [],
        "confirmed_entities": {},
        "workflow_step": None,
        "user_profile": {},
    }
