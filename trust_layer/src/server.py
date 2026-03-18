"""
trust_layer/src/server.py

FastAPI server wrapping BasicTrustLayer.
Port: 8003

Exposes:
  POST /check/input    — check user input against content rules
  POST /check/output   — check LLM response against output rules
  POST /check/consent  — verify consent for a write/identity connector
  GET  /health         — liveness probe
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from guardrails import BasicTrustLayer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class InputCheckRequest(BaseModel):
    session_id: str
    message: str


class OutputCheckRequest(BaseModel):
    session_id: str
    response: str


class ConsentCheckRequest(BaseModel):
    session_id: str
    connector_name: str


class TrustCheckResponse(BaseModel):
    passed: bool
    action: str
    reason: Optional[str] = None


class ConsentResponse(BaseModel):
    granted: bool


class StatusResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(trust: BasicTrustLayer) -> FastAPI:
    """
    Factory that wires the BasicTrustLayer instance into the FastAPI app.

    Args:
        trust: Pre-constructed BasicTrustLayer instance.

    Returns:
        Configured FastAPI application.
    """
    if trust is None:
        raise ValueError("trust must not be None")

    app = FastAPI(
        title="Trust Layer Service",
        description="Safety and compliance gate for the DPG AI framework.",
        version="0.1.0",
    )

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    @app.post("/check/input")
    def check_input(request: InputCheckRequest) -> TrustCheckResponse:
        """Evaluate raw user input against content rules and topic firewall."""
        start = time.time()
        session_id = request.session_id

        try:
            result = trust.check_input(session_id, request.message)
            logger.info(
                "trust_server.check_input",
                extra={
                    "operation": "server.check_input",
                    "status": "success",
                    "session_id": session_id,
                    "action": result.get("action", "allow"),
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return TrustCheckResponse(
                passed=result.get("passed", True),
                action=result.get("action", "allow"),
                reason=result.get("reason"),
            )

        except Exception as e:
            logger.error(
                "trust_server.check_input_error",
                extra={
                    "operation": "server.check_input",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            # Fail-open: allow on error so as not to block the user
            return TrustCheckResponse(passed=True, action="allow")

    @app.post("/check/output")
    def check_output(request: OutputCheckRequest) -> TrustCheckResponse:
        """Evaluate LLM-generated response against output safety rules."""
        start = time.time()
        session_id = request.session_id

        try:
            result = trust.check_output(session_id, request.response)
            logger.info(
                "trust_server.check_output",
                extra={
                    "operation": "server.check_output",
                    "status": "success",
                    "session_id": session_id,
                    "action": result.get("action", "allow"),
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return TrustCheckResponse(
                passed=result.get("passed", True),
                action=result.get("action", "allow"),
                reason=result.get("reason"),
            )

        except Exception as e:
            logger.error(
                "trust_server.check_output_error",
                extra={
                    "operation": "server.check_output",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            # Fail-open: allow on error
            return TrustCheckResponse(passed=True, action="allow")

    @app.post("/check/consent")
    def check_consent(request: ConsentCheckRequest) -> ConsentResponse:
        """Verify that confirmed user consent exists for a write or identity connector."""
        start = time.time()
        session_id = request.session_id

        try:
            granted = trust.check_consent(session_id, request.connector_name)
            logger.info(
                "trust_server.check_consent",
                extra={
                    "operation": "server.check_consent",
                    "status": "success",
                    "session_id": session_id,
                    "connector_name": request.connector_name,
                    "granted": granted,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return ConsentResponse(granted=bool(granted))

        except Exception as e:
            logger.error(
                "trust_server.check_consent_error",
                extra={
                    "operation": "server.check_consent",
                    "status": "failure",
                    "session_id": session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            # Fail-open: grant on error
            return ConsentResponse(granted=True)

    @app.get("/health")
    def health() -> StatusResponse:
        """Liveness probe."""
        return StatusResponse(status="ok")

    return app
