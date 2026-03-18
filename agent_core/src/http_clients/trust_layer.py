"""
agent_core/src/trust_http_client.py

HTTP client for the Trust Layer service at port 8003.
Implements the same interface as TrustLayerBase.

Config reads from:
  trust_client.endpoint   (default "http://localhost:8003")
  trust_client.timeout_ms (default 2000)

Error handling (fail-open):
  - check_input / check_output: return TrustCheckResult(passed=True, action="allow")
  - check_consent: return True
  Never raises to the caller.
"""

from __future__ import annotations

import logging
import time

import httpx

from src.interfaces.trust_layer import TrustLayerBase
from src.models import TrustCheckResult

logger = logging.getLogger(__name__)


class TrustLayerHttpClient(TrustLayerBase):
    """
    HTTP client that calls the Trust Layer service.

    Implements the TrustLayerBase interface contract so it can be swapped
    with any other implementation without changing the orchestrator.

    Args:
        config: Full config dict. Reads trust_client.endpoint and
                trust_client.timeout_ms.
    """

    def __init__(self, config: dict) -> None:
        if config is None:
            raise ValueError("config must not be None")

        client_cfg = config.get("trust_client", {})
        self._endpoint: str = client_cfg.get("endpoint", "http://localhost:8003")
        self._timeout_s: float = client_cfg.get("timeout_ms", 2000) / 1000

        logger.info(
            "trust_http_client.init",
            extra={
                "operation": "trust_http_client.init",
                "status": "success",
                "endpoint": self._endpoint,
                "timeout_s": self._timeout_s,
            },
        )

    # ------------------------------------------------------------------
    # Public interface — mirrors TrustLayerBase
    # ------------------------------------------------------------------

    def check_input(self, session_id: str, user_message: str) -> TrustCheckResult:
        """
        Evaluate raw user input against content rules and topic firewall.
        Returns TrustCheckResult(passed=True, action="allow") on any failure.
        Never raises.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")

        start = time.time()

        try:
            response = httpx.post(
                f"{self._endpoint}/check/input",
                json={"session_id": session_id, "message": user_message or ""},
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            data = response.json()

            result = TrustCheckResult(
                passed=data.get("passed", True),
                action=data.get("action", "allow"),
                reason=data.get("reason"),
            )

            logger.info(
                "trust_http_client.check_input",
                extra={
                    "operation": "trust_http_client.check_input",
                    "status": "success",
                    "session_id": session_id,
                    "action": result.action,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return result

        except httpx.TimeoutException as e:
            logger.error(
                "trust_http_client.check_input_timeout",
                extra={
                    "operation": "trust_http_client.check_input",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"TimeoutException: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return TrustCheckResult(passed=True, action="allow")

        except httpx.HTTPStatusError as e:
            logger.error(
                "trust_http_client.check_input_http_error",
                extra={
                    "operation": "trust_http_client.check_input",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return TrustCheckResult(passed=True, action="allow")

        except Exception as e:
            logger.error(
                "trust_http_client.check_input_error",
                extra={
                    "operation": "trust_http_client.check_input",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return TrustCheckResult(passed=True, action="allow")

    def check_output(self, session_id: str, llm_response: str) -> TrustCheckResult:
        """
        Evaluate LLM-generated response against output safety rules.
        Returns TrustCheckResult(passed=True, action="allow") on any failure.
        Never raises.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")

        start = time.time()

        try:
            response = httpx.post(
                f"{self._endpoint}/check/output",
                json={"session_id": session_id, "response": llm_response or ""},
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            data = response.json()

            result = TrustCheckResult(
                passed=data.get("passed", True),
                action=data.get("action", "allow"),
                reason=data.get("reason"),
            )

            logger.info(
                "trust_http_client.check_output",
                extra={
                    "operation": "trust_http_client.check_output",
                    "status": "success",
                    "session_id": session_id,
                    "action": result.action,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return result

        except httpx.TimeoutException as e:
            logger.error(
                "trust_http_client.check_output_timeout",
                extra={
                    "operation": "trust_http_client.check_output",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"TimeoutException: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return TrustCheckResult(passed=True, action="allow")

        except httpx.HTTPStatusError as e:
            logger.error(
                "trust_http_client.check_output_http_error",
                extra={
                    "operation": "trust_http_client.check_output",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return TrustCheckResult(passed=True, action="allow")

        except Exception as e:
            logger.error(
                "trust_http_client.check_output_error",
                extra={
                    "operation": "trust_http_client.check_output",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return TrustCheckResult(passed=True, action="allow")

    def check_consent(self, session_id: str, connector_name: str) -> bool:
        """
        Verify that confirmed user consent exists for a write or identity connector.
        Returns True (fail-open) on any failure.
        Never raises.
        """
        if session_id is None:
            raise ValueError("session_id must not be None")

        start = time.time()

        try:
            response = httpx.post(
                f"{self._endpoint}/check/consent",
                json={"session_id": session_id, "connector_name": connector_name or ""},
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            data = response.json()
            granted = bool(data.get("granted", True))

            logger.info(
                "trust_http_client.check_consent",
                extra={
                    "operation": "trust_http_client.check_consent",
                    "status": "success",
                    "session_id": session_id,
                    "connector_name": connector_name,
                    "granted": granted,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return granted

        except httpx.TimeoutException as e:
            logger.error(
                "trust_http_client.check_consent_timeout",
                extra={
                    "operation": "trust_http_client.check_consent",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"TimeoutException: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return True

        except httpx.HTTPStatusError as e:
            logger.error(
                "trust_http_client.check_consent_http_error",
                extra={
                    "operation": "trust_http_client.check_consent",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"HTTP {e.response.status_code}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return True

        except Exception as e:
            logger.error(
                "trust_http_client.check_consent_error",
                extra={
                    "operation": "trust_http_client.check_consent",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return True
