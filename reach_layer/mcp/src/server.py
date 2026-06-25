"""reach_layer/mcp/src/server.py

FastAPI server for the MCP channel adapter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from reach_layer_base import DoneEvent, SentenceEvent
from src.mcp_reach import McpReachLayer

logger = logging.getLogger(__name__)


class CallToolRequest(BaseModel):
    """Request payload for invoking a tool turn."""
    session_id: str
    text: str


class CallToolResponse(BaseModel):
    """Response payload for a completed tool turn."""
    reply: str
    session_id: str
    finished: bool
    error_type: str | None = None
    """One of: timeout | upstream_error | internal_error | stream_timeout | stream_error.
    None on success."""
    error_message: str | None = None
    """Human-readable error description for the MCP host. None on success."""


async def _handle_call_tool(
    mcp_reach: McpReachLayer,
    session_id: str,
    text: str,
    tool_timeout_s: float = 30.0,
    fire_session_start: bool = True,
) -> dict[str, Any]:
    """Submit a tool call to Agent Core and aggregate the SSE stream.

    Wraps ``submit_input`` with typed exception handling and guards the
    ``subscribe_events`` aggregation loop with ``asyncio.wait_for`` so that
    a stalled or crashed Agent Core cannot cause an unbounded hang.

    Args:
        mcp_reach: Initialised McpReachLayer instance.
        session_id: Unique session identifier.
        text: Input text for the turn.
        tool_timeout_s: Maximum wall-clock seconds to wait for the full turn
            (submit + stream aggregation). Defaults to 30.0 s.
        fire_session_start: Whether to invoke ``on_session_start``. Callers
            pass ``False`` for subsequent turns in the same session so the
            hook fires only once per logical session.

    Returns:
        Dict with keys: reply, session_id, finished, error_type, error_message.
        On success: error_type and error_message are None.
        On failure: reply may be a partial result; finished is True so the
        MCP host knows the turn is over; error_type names the failure class.
    """
    if fire_session_start:
        await mcp_reach.on_session_start(session_id, "")

    # ------------------------------------------------------------------
    # submit_input — typed error handling
    # ------------------------------------------------------------------
    try:
        await mcp_reach.submit_input(session_id, text, user_id=None)
    except httpx.TimeoutException as exc:
        logger.error(
            "mcp_server.submit_input_timeout",
            extra={
                "operation": "mcp_server.submit_input",
                "status": "failure",
                "session_id": session_id,
                "error": str(exc),
            },
        )
        return {
            "reply": "",
            "session_id": session_id,
            "finished": True,
            "error_type": "timeout",
            "error_message": "Agent Core did not respond in time.",
        }
    except httpx.HTTPStatusError as exc:
        logger.error(
            "mcp_server.submit_input_http_error",
            extra={
                "operation": "mcp_server.submit_input",
                "status": "failure",
                "session_id": session_id,
                "error": str(exc),
            },
        )
        return {
            "reply": "",
            "session_id": session_id,
            "finished": True,
            "error_type": "upstream_error",
            "error_message": f"Agent Core returned {exc.response.status_code}.",
        }
    except Exception as exc:
        logger.error(
            "mcp_server.submit_input_unexpected",
            extra={
                "operation": "mcp_server.submit_input",
                "status": "failure",
                "session_id": session_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return {
            "reply": "",
            "session_id": session_id,
            "finished": True,
            "error_type": "internal_error",
            "error_message": "Unexpected error submitting to Agent Core.",
        }

    # ------------------------------------------------------------------
    # subscribe_events — bounded by asyncio.wait_for
    #
    # Note: the base-class subscribe_events already yields a synthetic
    # DoneEvent on SSE-level exceptions (reach_layer_base.py:314), which
    # handles most Agent Core restart/crash scenarios. asyncio.wait_for
    # is the additional guard for the edge case where the generator stalls
    # without raising or yielding (e.g. a network partition that keeps the
    # TCP connection half-open indefinitely).
    # ------------------------------------------------------------------
    parts: list[str] = []
    finished: bool = False

    async def _aggregate() -> None:
        nonlocal finished
        async for event in mcp_reach.subscribe_events(session_id):
            if isinstance(event, SentenceEvent):
                parts.append(event.text)
            elif isinstance(event, DoneEvent):
                finished = event.session_ended
                break

    try:
        await asyncio.wait_for(_aggregate(), timeout=tool_timeout_s)
    except asyncio.TimeoutError:
        logger.error(
            "mcp_server.subscribe_events_timeout",
            extra={
                "operation": "mcp_server.subscribe_events",
                "status": "failure",
                "session_id": session_id,
                "tool_timeout_s": tool_timeout_s,
            },
        )
        return {
            "reply": " ".join(parts).strip(),
            "session_id": session_id,
            "finished": True,
            "error_type": "stream_timeout",
            "error_message": (
                f"Agent Core stream did not complete within {tool_timeout_s}s."
            ),
        }
    except Exception as exc:
        logger.error(
            "mcp_server.subscribe_events_error",
            extra={
                "operation": "mcp_server.subscribe_events",
                "status": "failure",
                "session_id": session_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return {
            "reply": " ".join(parts).strip(),
            "session_id": session_id,
            "finished": True,
            "error_type": "stream_error",
            "error_message": "Error reading Agent Core response stream.",
        }

    if finished:
        await mcp_reach.on_session_end(session_id)

    return {
        "reply": " ".join(parts).strip(),
        "session_id": session_id,
        "finished": finished,
        "error_type": None,
        "error_message": None,
    }


def create_app(mcp_reach: McpReachLayer, config: dict) -> FastAPI:
    """Create and return the FastAPI application.

    Args:
        mcp_reach: Initialised McpReachLayer instance.
        config: Full merged config dict.

    Returns:
        Configured FastAPI application.

    Raises:
        ValueError: If mcp_reach is None.
    """
    if mcp_reach is None:
        raise ValueError("mcp_reach must not be None")

    mcp_cfg = (
        config.get("reach_layer", {}).get("channels", {}).get("mcp", {})
        if config else {}
    )
    tool_timeout_s: float = float(mcp_cfg.get("tool_timeout_s", 30.0))

    app = FastAPI(title="Reach Layer — MCP Channel Adapter")

    # Track which session IDs have already received on_session_start so the
    # hook fires exactly once per logical session, not once per tool call.
    _active_sessions: set[str] = set()

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return service health status.

        Returns:
            Dict containing status "ok".
        """
        return {"status": "ok"}

    @app.post("/call_tool", response_model=CallToolResponse)
    async def call_tool(req: CallToolRequest) -> dict[str, Any]:
        """Handle incoming tool invocation requests.

        Fires ``on_session_start`` only on the first call for a given
        ``session_id``; subsequent calls in the same multi-turn session
        skip the hook. Removes the session from the active-sessions set
        when ``finished=True`` so that a future reconnect with the same ID
        triggers ``on_session_start`` again.

        Args:
            req: CallToolRequest payload.

        Returns:
            Dict containing reply text, session ID, finished flag, and
            optional error_type / error_message fields.
        """
        is_new_session = req.session_id not in _active_sessions
        if is_new_session:
            _active_sessions.add(req.session_id)

        result = await _handle_call_tool(
            mcp_reach,
            req.session_id,
            req.text,
            tool_timeout_s=tool_timeout_s,
            fire_session_start=is_new_session,
        )

        # Clean up the session tracker so a future call with the same
        # session_id (after reconnect) fires on_session_start again.
        if result.get("finished"):
            _active_sessions.discard(req.session_id)

        return result

    return app
