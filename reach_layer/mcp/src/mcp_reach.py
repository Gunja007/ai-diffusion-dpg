"""reach_layer/mcp/src/mcp_reach.py

MCP channel adapter implementation extending TextChannelBase.
"""

from __future__ import annotations

import logging

from reach_layer_base import TextChannelBase

logger = logging.getLogger(__name__)


class McpReachLayer(TextChannelBase):
    """MCP channel adapter.

    Bridges tool calls from MCP hosts (e.g., Claude Desktop, Cursor)
    to the Agent Core orchestrator.
    """

    def __init__(self, config: dict) -> None:
        """Initialise the McpReachLayer with configuration.

        Args:
            config: Full merged reach_layer config dict.

        Raises:
            ValueError: If config is None.
        """
        super().__init__(config, channel_name="mcp")
        mcp_cfg = (
            config.get("reach_layer", {}).get("channels", {}).get("mcp", {})
            if config else {}
        )
        self._port: int = mcp_cfg.get("port", 8007)
        logger.info(
            "mcp_reach.init",
            extra={
                "operation": "mcp_reach.init",
                "status": "success",
                "channel": "mcp",
                "assembly_mode": self.assembly_mode,
                "port": self._port,
            },
        )

    async def run_loop(self) -> None:
        """No-op placeholder for satisfying TextChannelBase.

        MCP channel is server-driven; the actual event loop is owned by
        FastAPI and the uvicorn server in server.py.
        """
        logger.info(
            "mcp_reach.run_loop_noop",
            extra={
                "operation": "mcp_reach.run_loop",
                "status": "skipped",
                "reason": "mcp channel is server-driven; server.py owns the loop",
            },
        )

    async def on_session_start(self, session_id: str, user_id: str) -> None:
        """Log MCP connection start.

        Args:
            session_id: Unique session identifier.
            user_id: Optional user identifier (treated as anonymous if empty).
        """
        if not session_id or not session_id.strip():
            logger.warning(
                "mcp_reach.session_start_invalid",
                extra={
                    "operation": "mcp_reach.on_session_start",
                    "status": "skipped",
                    "reason": "session_id is empty",
                },
            )
            return
        logger.info(
            "mcp_reach.session_start",
            extra={
                "operation": "mcp_reach.on_session_start",
                "status": "success",
                "session_id": session_id,
                "user_id": user_id or "anonymous",
            },
        )

    async def on_session_end(self, session_id: str) -> None:
        """Log MCP connection end.

        Args:
            session_id: Unique session identifier.
        """
        if not session_id or not session_id.strip():
            logger.warning(
                "mcp_reach.session_end_invalid",
                extra={
                    "operation": "mcp_reach.on_session_end",
                    "status": "skipped",
                    "reason": "session_id is empty",
                },
            )
            return
        logger.info(
            "mcp_reach.session_end",
            extra={
                "operation": "mcp_reach.on_session_end",
                "status": "success",
                "session_id": session_id,
            },
        )
