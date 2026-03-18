"""
agent_core/src/llm_proxy_server.py

FastAPI application exposing the internal LLM proxy endpoint.

NOTE: This server is NOT currently used. Language Normalisation and NLU Processor
have been moved into Agent Core and call the LLM wrapper directly. The proxy
endpoint is retained so that other DPG layers (e.g. Trust Layer, Action Gateway)
can use it in the future without requiring their own Anthropic API key.

Any DPG that needs LLM access calls this endpoint instead of holding an Anthropic
API key itself. Agent Core remains the sole owner of the key and the sole caller
of the Anthropic API.

Endpoint:
    POST /internal/llm/call   — proxy an LLM call through ClaudeLLMWrapper
    GET  /health               — readiness probe
"""

import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.llm_wrapper.base import LLMWrapperBase
from src.models import LLMResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response Pydantic schemas
# ---------------------------------------------------------------------------

class ToolCallSchema(BaseModel):
    tool_name: str
    tool_use_id: str
    input_params: dict[str, Any]


class LLMCallRequest(BaseModel):
    messages: list[dict]
    tools: list[dict] = []
    system: str = ""
    model_override: Optional[str] = None


class LLMCallResponse(BaseModel):
    content: Optional[str]
    tool_calls: list[ToolCallSchema] = []
    stop_reason: str
    model_used: str
    input_tokens: int
    output_tokens: int


class HealthResponse(BaseModel):
    status: str
    active_model: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(llm: LLMWrapperBase) -> FastAPI:
    """
    Create and return the FastAPI application with the LLM wrapper bound to it.

    Args:
        llm: The LLMWrapperBase instance (ClaudeLLMWrapper in production).
             Stored on app.state so the endpoint can access it without globals.

    Called once at startup from main.py.
    """
    if llm is None:
        raise ValueError("llm must not be None")

    app = FastAPI(
        title="Agent Core — LLM Proxy",
        description="Internal LLM proxy endpoint for DPG services.",
        version="0.1.0",
        docs_url="/docs",
    )
    app.state.llm = llm

    # ----------------------------------------------------------------
    # POST /internal/llm/call
    # ----------------------------------------------------------------

    @app.post("/internal/llm/call", response_model=LLMCallResponse)
    def llm_call(request: LLMCallRequest) -> LLMCallResponse:
        """
        Proxy an LLM call to the Anthropic API via ClaudeLLMWrapper.

        The caller (e.g. Knowledge Engine's HttpLLMWrapper) sends messages,
        optional tools, an optional system prompt, and an optional model override.
        Agent Core forwards the call, applies retry/fallback logic, and returns
        the normalised LLMResponse as JSON.

        Returns HTTP 422 if messages list is empty.
        Returns stop_reason="error" in the body if the LLM call itself fails —
        never returns HTTP 500 for LLM-level failures.
        """
        if not request.messages:
            raise HTTPException(status_code=422, detail="messages must not be empty")

        start = time.time()

        response: LLMResponse = app.state.llm.call(
            messages=request.messages,
            tools=request.tools,
            system=request.system,
            model_override=request.model_override,
        )

        latency_ms = int((time.time() - start) * 1000)
        status = "failure" if response.stop_reason == "error" else "success"

        logger.info(
            "llm_proxy.call",
            extra={
                "operation": "llm_proxy_server.llm_call",
                "status": status,
                "model": response.model_used,
                "latency_ms": latency_ms,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "stop_reason": response.stop_reason,
            },
        )

        return LLMCallResponse(
            content=response.content,
            tool_calls=[
                ToolCallSchema(
                    tool_name=tc.tool_name,
                    tool_use_id=tc.tool_use_id,
                    input_params=tc.input_params,
                )
                for tc in response.tool_calls
            ],
            stop_reason=response.stop_reason,
            model_used=response.model_used,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    # ----------------------------------------------------------------
    # GET /health
    # ----------------------------------------------------------------

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """
        Readiness probe. Returns the currently active model name so callers
        can confirm which model Agent Core is routing to.
        """
        return HealthResponse(
            status="ok",
            active_model=app.state.llm.get_active_model(),
        )

    return app
