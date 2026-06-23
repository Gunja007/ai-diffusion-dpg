"""reach_layer/mcp/src/server.py

FastAPI server for the MCP channel adapter.
"""

from __future__ import annotations

import logging
from typing import Any

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


async def _handle_call_tool(
    mcp_reach: McpReachLayer,
    session_id: str,
    text: str,
) -> dict[str, Any]:
    """Submit a tool call to Agent Core, aggregate the SSE stream.

    Args:
        mcp_reach: Initialised McpReachLayer instance.
        session_id: Unique session identifier.
        text: Input text for the turn.

    Returns:
        Dict with keys: reply, session_id, finished.
    """
    await mcp_reach.on_session_start(session_id, "")

    await mcp_reach.submit_input(session_id, text, user_id=None)

    parts: list[str] = []
    finished: bool = False
    async for event in mcp_reach.subscribe_events(session_id):
        if isinstance(event, SentenceEvent):
            parts.append(event.text)
        elif isinstance(event, DoneEvent):
            # Wire session_ended → finished so callers can detect conversation end.
            finished = event.session_ended
            break

    if finished:
        await mcp_reach.on_session_end(session_id)

    return {
        "reply": " ".join(parts).strip(),
        "session_id": session_id,
        "finished": finished,
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

    app = FastAPI(title="Reach Layer — MCP Channel Adapter")

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

        Args:
            req: CallToolRequest payload.

        Returns:
            Dict containing reply text, session ID, and finished flag.
        """
        return await _handle_call_tool(mcp_reach, req.session_id, req.text)

    return app
