"""
agent_core/src/ke_http_client.py

HttpKnowledgeEngineClient — HTTP client for the Knowledge Engine service.

Implements KnowledgeEngineBase. Makes HTTP POST calls to the Knowledge Engine
FastAPI service (default: http://localhost:8001/assemble_prompt).

Used by run.py to wire the PoC end-to-end. In production this is replaced
by the same HTTP client pointing at a deployed KE service instance.

Error handling:
- Timeout: returns an empty messages list with a fallback user turn.
- HTTP 4xx/5xx: logs and returns minimal fallback messages.
- Any other exception: logs and returns minimal fallback messages.
The orchestrator handles an empty messages list gracefully.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from src.models import SessionState

logger = logging.getLogger(__name__)


class HttpKnowledgeEngineClient:
    """
    HTTP client that calls the Knowledge Engine service at /assemble_prompt.

    Implements the KnowledgeEngineBase interface contract:
        assemble_prompt(...) -> list[dict]

    Args:
        config: Full config dict. Reads ke_client.endpoint and ke_client.timeout_ms.
    """

    def __init__(self, config: dict) -> None:
        if config is None:
            raise ValueError("config must not be None")

        ke_cfg = config.get("ke_client", {})
        self._endpoint: str = ke_cfg.get(
            "endpoint", "http://localhost:8001/assemble_prompt"
        )
        self._timeout_s: float = ke_cfg.get("timeout_ms", 8000) / 1000

        logger.info(
            "ke_http_client.init",
            extra={
                "operation": "ke_http_client.init",
                "status": "success",
                "endpoint": self._endpoint,
                "timeout_s": self._timeout_s,
            },
        )

    def assemble_prompt(
        self,
        session_id: str,
        user_message: str,
        session_state: SessionState,
        normalised_input: str = "",
        detected_language: str = "",
        intent: str = "unknown",
        entities: Optional[dict[str, Any]] = None,
        sentiment: str = "neutral",
        confidence: float = 0.0,
    ) -> tuple[list[dict], str]:
        """
        Call the Knowledge Engine service to assemble the prompt.

        Returns (messages, system) ready for the LLM call.
        Returns ([], "") on any failure — orchestrator handles empty messages gracefully.
        Never raises.
        """
        if not user_message:
            return [], ""

        start = time.time()

        payload = {
            "session_id": session_id,
            "user_message": user_message,
            "session_state": {
                "session_id": session_state.session_id,
                "history": session_state.history,
                "confirmed_entities": session_state.confirmed_entities,
                "workflow_step": session_state.workflow_step,
                "user_profile": session_state.user_profile,
            },
            "normalised_input": normalised_input,
            "detected_language": detected_language,
            "intent": intent,
            "entities": entities or {},
            "sentiment": sentiment,
            "confidence": confidence,
        }

        try:
            response = httpx.post(
                self._endpoint,
                json=payload,
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            data = response.json()
            messages = data.get("messages", [])
            system = data.get("system", "")

            logger.info(
                "ke_http_client.assemble_prompt",
                extra={
                    "operation": "ke_http_client.assemble_prompt",
                    "status": "success",
                    "session_id": session_id,
                    "message_count": len(messages),
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return messages, system

        except httpx.TimeoutException as e:
            logger.error(
                "ke_http_client.timeout",
                extra={
                    "operation": "ke_http_client.assemble_prompt",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"TimeoutException: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return [], ""

        except httpx.HTTPStatusError as e:
            logger.error(
                "ke_http_client.http_error",
                extra={
                    "operation": "ke_http_client.assemble_prompt",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return [], ""

        except Exception as e:
            logger.error(
                "ke_http_client.unexpected_error",
                extra={
                    "operation": "ke_http_client.assemble_prompt",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return [], ""
