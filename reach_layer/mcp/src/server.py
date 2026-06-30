"""reach_layer/mcp/src/server.py

FastAPI server for the MCP channel adapter.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict
import hmac
import json
import logging
from typing import Any, Callable, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

import mcp.types as types
from mcp.server import Server
from mcp.server.lowlevel.server import RequestContext
from mcp.server.sse import SseServerTransport

from reach_layer_base import DoneEvent, SentenceEvent
from src.mcp_reach import McpReachLayer

logger = logging.getLogger(__name__)

# Request-scoped context variable to propagate authenticated caller ID
current_caller_agent_id: ContextVar[str] = ContextVar("current_caller_agent_id", default="anonymous")


class CallToolRequest(BaseModel):
    """Request payload for invoking a tool turn."""
    session_id: str
    text: str
    locale: str | None = None
    metadata: dict | None = None


class CallToolResponse(BaseModel):
    """Response payload for a completed tool turn."""
    reply: str
    session_id: str
    finished: bool
    events: list[dict] = Field(default_factory=list)
    error_type: str | None = None
    """One of: timeout | upstream_error | internal_error | stream_timeout | stream_error.
    None on success."""
    error_message: str | None = None
    """Human-readable error description for the MCP host. None on success."""


def _authenticate_request(request: Request, callers: list[Any]) -> str:
    """Validate request API key and return matching caller_agent_id.

    Raises:
        HTTPException(503): If no callers are configured (service not ready).
        HTTPException(401): If the Authorization header is absent or invalid.

    API keys must be supplied via the ``Authorization: Bearer <key>`` header.
    Query-parameter auth (``?api_key=``) is not supported; it leaks credentials
    into HTTP access logs and proxy histories.
    """
    if not callers:
        raise HTTPException(
            status_code=503,
            detail=(
                "MCP channel has no authorised callers configured. "
                "Add at least one entry to reach_layer.channels.mcp.callers[] "
                "before exposing this endpoint."
            ),
        )

    api_key: str | None = None

    # Only accept Authorization: Bearer <key> — query-param auth is not supported.
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        api_key = auth_header[7:].strip()

    if not api_key:
        raise HTTPException(status_code=401, detail="API key is missing")

    for caller in callers:
        caller_key = getattr(caller, "api_key", None) or (caller.get("api_key") if isinstance(caller, dict) else None)
        caller_id = getattr(caller, "caller_agent_id", None) or (caller.get("caller_agent_id") if isinstance(caller, dict) else None)
        # Use constant-time comparison to prevent timing side-channel attacks.
        if caller_key and hmac.compare_digest(caller_key, api_key) and caller_id:
            return caller_id

    raise HTTPException(status_code=401, detail="Invalid API key")


async def _handle_call_tool(
    mcp_reach: McpReachLayer,
    session_id: str,
    text: str,
    tool_timeout_s: float = 30.0,
    fire_session_start: bool = True,
    caller_agent_id: Optional[str] = None,
    progress_callback: Optional[Callable[[SentenceEvent], Any]] = None,
    locale: Optional[str] = None,
    metadata: Optional[dict] = None,
    include_events: bool = True,
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
        caller_agent_id: Authenticated caller ID.
        progress_callback: Optional async callback to stream sentence events.
        locale: Optional language locale.
        metadata: Optional caller metadata.

    Returns:
        Dict with keys: reply, session_id, finished, events, error_type, error_message.
        On success: error_type and error_message are None.
        On failure: reply may be a partial result; finished is True so the
        MCP host knows the turn is over; error_type names the failure class.
    """
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        otel_trace = None

    if otel_trace:
        tracer = otel_trace.get_tracer(__name__)
        ctx_manager = tracer.start_as_current_span("reach.inbound")
    else:
        from contextlib import nullcontext
        ctx_manager = nullcontext()

    with ctx_manager as span:
        if otel_trace and span:
            span.set_attribute("session_id", session_id or "")
            span.set_attribute("dpg.channel", "mcp")
            span.set_attribute("dpg.assembly_mode", mcp_reach.assembly_mode)
            if caller_agent_id:
                span.set_attribute("peer.agent_id", caller_agent_id)
                span.set_attribute("peer.protocol", "mcp")
                span.set_attribute("peer.direction", "inbound")

        if fire_session_start:
            await mcp_reach.on_session_start(session_id, "")

        # ------------------------------------------------------------------
        # submit_input — typed error handling
        # ------------------------------------------------------------------
        try:
            await mcp_reach.submit_input(
                session_id, text, user_id=None, caller_agent_id=caller_agent_id,
                locale=locale, metadata=metadata
            )
        except httpx.TimeoutException as exc:
            if otel_trace and span:
                span.record_exception(exc)
                span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
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
                "events": [],
                "error_type": "timeout",
                "error_message": "Agent Core did not respond in time.",
            }
        except httpx.HTTPStatusError as exc:
            if otel_trace and span:
                span.record_exception(exc)
                span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
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
                "events": [],
                "error_type": "upstream_error",
                "error_message": f"Agent Core returned {exc.response.status_code}.",
            }
        except Exception as exc:
            if otel_trace and span:
                span.record_exception(exc)
                span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
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
                "events": [],
                "error_type": "internal_error",
                "error_message": "Unexpected error submitting to Agent Core.",
            }

        # ------------------------------------------------------------------
        # subscribe_events — bounded by asyncio.wait_for
        # ------------------------------------------------------------------
        parts: list[str] = []
        events_list: list[dict] = []
        finished: bool = False
        error_type: str | None = None
        error_message: str | None = None

        async def _aggregate() -> None:
            nonlocal finished, error_type, error_message
            async for event in mcp_reach.subscribe_events(session_id):
                if include_events:
                    events_list.append(asdict(event))
                if isinstance(event, SentenceEvent):
                    parts.append(event.text)
                    if progress_callback:
                        try:
                            if asyncio.iscoroutinefunction(progress_callback):
                                await progress_callback(event)
                            else:
                                progress_callback(event)
                        except Exception as pe:
                            logger.warning("mcp_server.progress_callback_error", extra={"error": str(pe)})
                elif isinstance(event, DoneEvent):
                    finished = event.session_ended
                    error_type = event.error_type
                    error_message = event.error_message
                    break

        try:
            await asyncio.wait_for(_aggregate(), timeout=tool_timeout_s)
        except asyncio.TimeoutError as exc:
            if otel_trace and span:
                span.record_exception(exc)
                span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
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
                "events": events_list,
                "error_type": "stream_timeout",
                "error_message": (
                    f"Agent Core stream did not complete within {tool_timeout_s}s."
                ),
            }
        except Exception as exc:
            if otel_trace and span:
                span.record_exception(exc)
                span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR, str(exc)))
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
                "events": events_list,
                "error_type": "stream_error",
                "error_message": "Error reading Agent Core response stream.",
            }

        if finished:
            await mcp_reach.on_session_end(session_id)

        return {
            "reply": " ".join(parts).strip(),
            "session_id": session_id,
            "finished": finished,
            "events": events_list,
            "error_type": error_type,
            "error_message": error_message,
        }


class MessagesASGIApp:
    """Raw ASGI application to handle client messages for the SSE transport.

    Using a raw ASGI application class bypasses FastAPI's standard route handler
    response wrapping, preventing 'http.response.start' duplicate message errors.
    """
    def __init__(self, callers: list[Any], sse: SseServerTransport) -> None:
        self.callers = callers
        self.sse = sse

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        request = Request(scope, receive)
        caller_id = _authenticate_request(request, self.callers)
        token = current_caller_agent_id.set(caller_id)
        try:
            await self.sse.handle_post_message(scope, receive, send)
        finally:
            current_caller_agent_id.reset(token)


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
    callers = mcp_cfg.get("callers", [])

    app = FastAPI(title="Reach Layer — MCP Channel Adapter")

    mcp_server = Server("dpg-mcp")
    sse = SseServerTransport("/messages")

    # Track which session IDs have already received on_session_start
    _active_sessions: set[str] = set()

    @mcp_server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """Expose dpg.send_message to the MCP host."""
        return [
            types.Tool(
                name="dpg.send_message",
                description="Send a message to the DPG agent session.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Unique session identifier"
                        },
                        "message": {
                            "type": "string",
                            "description": "Message to send to the agent"
                        },
                        "locale": {
                            "type": "string",
                            "description": "Locale for the session (optional)"
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Arbitrary metadata key-value pairs (optional)"
                        },
                        "caller_agent_id": {
                            "type": "string",
                            "description": (
                                "Deprecated: this field is ignored by the server. "
                                "The caller identity is always derived from the "
                                "Authorization: Bearer header and cannot be overridden "
                                "by the client."
                            )
                        },
                    },
                    "required": ["session_id", "message"],
                }
            )
        ]

    @mcp_server.call_tool()
    async def handle_call_tool(
        name: str,
        arguments: dict | None,
        ctx: RequestContext | None = None
    ) -> list[types.TextContent]:
        """Handle JSON-RPC call_tool requests."""
        if name != "dpg.send_message":
            raise ValueError(f"Unknown tool: {name}")

        if not arguments:
            raise ValueError("Arguments must be provided")

        session_id = arguments.get("session_id")
        text = arguments.get("message")
        locale = arguments.get("locale")
        metadata = arguments.get("metadata")
        if not session_id or not text:
            raise ValueError("session_id and message must not be empty")

        if ctx is None:
            try:
                ctx = mcp_server.request_context
            except LookupError:
                ctx = None

        # caller_id is always auth-derived from _authenticate_request via the
        # ContextVar set on /sse, /messages, and /call_tool. The client-supplied
        # caller_agent_id argument is intentionally ignored to prevent forgery.
        caller_id = current_caller_agent_id.get()

        # Namespace session_id by authenticated caller to prevent cross-caller
        # session collision.
        prefix = f"{caller_id}:" if caller_id else ""
        namespaced_session_id = session_id
        if prefix and not session_id.startswith(prefix):
            namespaced_session_id = f"{prefix}{session_id}"

        is_new_session = namespaced_session_id not in _active_sessions
        if is_new_session:
            _active_sessions.add(namespaced_session_id)

        progress_token = None
        if ctx and getattr(ctx, "meta", None) is not None:
            meta = ctx.meta
            if isinstance(meta, dict):
                progress_token = meta.get("progressToken")
            else:
                progress_token = getattr(meta, "progressToken", None)

        async def progress_callback(event: SentenceEvent) -> None:
            if progress_token and ctx and ctx.session:
                await ctx.session.send_notification(
                    types.ProgressNotification(
                        method="notifications/progress",
                        params=types.ProgressNotificationParams(
                            progressToken=progress_token,
                            progress=float(event.sentence_index),
                            meta=types.NotificationParams.Meta(
                                extra={
                                    "text": event.text,
                                    "sentence_index": event.sentence_index,
                                }
                            )
                        )
                    )
                )

        # Optimize by not returning full event list to standard MCP tool callers
        include_events = False
        if metadata and metadata.get("include_events"):
            include_events = True

        result = await _handle_call_tool(
            mcp_reach,
            namespaced_session_id,
            text,
            tool_timeout_s=tool_timeout_s,
            fire_session_start=is_new_session,
            caller_agent_id=caller_id,
            progress_callback=progress_callback,
            locale=locale,
            metadata=metadata,
            include_events=include_events,
        )

        if result.get("finished"):
            _active_sessions.discard(namespaced_session_id)

        # Restore original un-namespaced session_id for the client
        result["session_id"] = session_id

        return [
            types.TextContent(
                type="text",
                text=json.dumps(result)
            )
        ]

    app.state.handle_call_tool = handle_call_tool

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return service health status."""
        return {"status": "ok"}

    @app.get("/sse")
    async def handle_sse(request: Request):
        """Establish standard MCP SSE communication stream."""
        caller_id = _authenticate_request(request, callers)
        token = current_caller_agent_id.set(caller_id)
        try:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read_stream, write_stream):
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options(),
                )
        finally:
            current_caller_agent_id.reset(token)

    # Register raw ASGI app for messages to bypass FastAPI response wrapping
    app.add_route("/messages", MessagesASGIApp(callers, sse), methods=["POST"])

    @app.post("/call_tool", response_model=CallToolResponse)
    async def call_tool(req: CallToolRequest, request: Request) -> dict[str, Any]:
        """Backward-compatible REST tool call endpoint.

        Requires ``Authorization: Bearer <key>`` when callers are configured.
        Returns 503 if no callers are configured (service not ready).
        """
        caller_id = _authenticate_request(request, callers)

        token = current_caller_agent_id.set(caller_id)
        try:
            prefix = f"{caller_id}:" if caller_id else ""
            namespaced_session_id = req.session_id
            if prefix and not req.session_id.startswith(prefix):
                namespaced_session_id = f"{prefix}{req.session_id}"

            is_new_session = namespaced_session_id not in _active_sessions
            if is_new_session:
                _active_sessions.add(namespaced_session_id)

            result = await _handle_call_tool(
                mcp_reach,
                namespaced_session_id,
                req.text,
                tool_timeout_s=tool_timeout_s,
                fire_session_start=is_new_session,
                caller_agent_id=caller_id,
                locale=req.locale,
                metadata=req.metadata,
                include_events=True,  # Always include events for backward-compatible REST endpoint
            )

            if result.get("finished"):
                _active_sessions.discard(namespaced_session_id)

            # Restore original un-namespaced session_id for the client
            result["session_id"] = req.session_id

            return result
        finally:
            current_caller_agent_id.reset(token)

    return app
