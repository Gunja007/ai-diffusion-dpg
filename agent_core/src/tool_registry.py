"""
agent_core/tool_registry.py

Builds and caches the tool definitions injected into every LLM call.
Reads connector definitions from the domain config at startup, fetches the tool
schema from Action Gateway, and serves the merged list on every turn.

Also responsible for reporting which tools require user consent before execution.
"""

from __future__ import annotations

import logging

from src.exceptions import ConfigurationError
from src.interfaces.action_gateway import ActionGatewayBase

logger = logging.getLogger(__name__)

# Connector types that require explicit user consent before execution
_CONSENT_REQUIRED_TYPES = {"write", "identity"}


class ToolRegistry:
    """
    Initialised once at startup. Thread-safe for concurrent reads after init.

    Args:
        config:   Domain configuration dict. Must contain a "connectors" key
                  with sub-keys "read", "write", and/or "identity", each a list
                  of connector definition dicts with at least a "name" field.
        gateway:  Action Gateway instance. list_available_tools() is called once
                  here to fetch Anthropic-formatted tool definitions.
    """

    def __init__(self, config: dict, gateway: ActionGatewayBase) -> None:
        if config is None:
            raise ValueError("config must not be None")
        if gateway is None:
            raise ValueError("gateway must not be None")

        self._consent_tools: set[str] = self._build_consent_set(config)
        self._tool_definitions: list[dict] = self._load_and_validate(config, gateway)

        logger.info(
            "tool_registry.init",
            extra={
                "operation": "tool_registry.init",
                "status": "success",
                "tool_count": len(self._tool_definitions),
                "consent_required_tools": list(self._consent_tools),
            },
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_tool_definitions(self) -> list[dict]:
        """
        Return tool definitions in Anthropic tool-use format.
        Cached — safe and cheap to call on every LLM request.
        """
        return self._tool_definitions

    def get_tool_names(self) -> set[str]:
        """Return the set of valid tool names for response validation."""
        return {t["name"] for t in self._tool_definitions}

    def requires_consent(self, tool_name: str) -> bool:
        """
        Return True if the tool is of type 'write' or 'identity'.
        ManagerAgent checks this before executing any tool call.
        """
        return tool_name in self._consent_tools

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_consent_set(self, config: dict) -> set[str]:
        """Collect tool names that require consent from write/identity connector defs."""
        consent_tools: set[str] = set()
        connectors = config.get("connectors", {})

        for connector_type, connector_list in connectors.items():
            if connector_type in _CONSENT_REQUIRED_TYPES:
                for connector in connector_list or []:
                    name = connector.get("name")
                    if name:
                        consent_tools.add(name)

        return consent_tools

    def _load_and_validate(
        self, config: dict, gateway: ActionGatewayBase
    ) -> list[dict]:
        """
        Fetch tool definitions from the gateway and validate them against config.
        Raises ConfigurationError if a tool definition is missing a required field
        or if a connector defined in config has no matching tool definition.
        """
        try:
            definitions = gateway.list_available_tools()
        except Exception as e:
            raise ConfigurationError(
                f"Failed to load tool definitions from Action Gateway: {e}"
            ) from e

        if not isinstance(definitions, list):
            raise ConfigurationError(
                f"Action Gateway returned unexpected type for tool definitions: "
                f"{type(definitions)}"
            )

        defined_names: set[str] = set()
        for tool_def in definitions:
            name = tool_def.get("name")
            if not name:
                raise ConfigurationError(
                    f"Tool definition missing required 'name' field: {tool_def}"
                )
            defined_names.add(name)

        # Verify every connector in config has a matching tool definition
        connectors = config.get("connectors", {})
        for connector_type, connector_list in connectors.items():
            for connector in connector_list or []:
                connector_name = connector.get("name")
                if connector_name and connector_name not in defined_names:
                    raise ConfigurationError(
                        f"Connector '{connector_name}' (type: {connector_type}) defined in "
                        f"config has no matching tool definition from Action Gateway."
                    )

        return definitions
