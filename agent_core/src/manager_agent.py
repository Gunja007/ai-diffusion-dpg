"""
agent_core/manager_agent.py

Owns the LLM → tool → LLM loop for a single turn.
Called by orchestrator after the first LLM call. Drives the tool-use cycle,
enforces the consent gate for write/identity connectors, and returns the
final response text once the loop is complete.

ManagerAgent never calls the LLM or external systems autonomously —
it always acts on an initial LLMResponse passed in by the orchestrator.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from src.exceptions import ConsentRequiredError
from src.interfaces.action_gateway import ActionGatewayBase
from src.interfaces.trust_layer import TrustLayerBase
from src.llm_wrapper.base import LLMWrapperBase
from src.models import LLMResponse, ToolCall, ToolResult
from src.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


class ManagerAgent:
    """
    Drives the tool-use loop for one conversation turn.

    Args:
        llm_wrapper:    Used for the second (and any subsequent) LLM call after tool results.
        tool_registry:  Used to check which tools require consent.
        action_gateway: Executes tool calls against external connectors.
        trust_layer:    Used to verify consent before write/identity tool execution.
        max_tool_rounds:Maximum tool → LLM cycles per turn. Default 1 for PoC.
                        Configurable so extending to multi-step chains needs only a config change.
    """

    def __init__(
        self,
        llm_wrapper: LLMWrapperBase,
        tool_registry: ToolRegistry,
        action_gateway: ActionGatewayBase,
        trust_layer: TrustLayerBase,
        max_tool_rounds: int = 1,
    ) -> None:
        if llm_wrapper is None:
            raise ValueError("llm_wrapper must not be None")
        if tool_registry is None:
            raise ValueError("tool_registry must not be None")
        if action_gateway is None:
            raise ValueError("action_gateway must not be None")
        if trust_layer is None:
            raise ValueError("trust_layer must not be None")

        self._llm = llm_wrapper
        self._registry = tool_registry
        self._gateway = action_gateway
        self._trust = trust_layer
        self._max_tool_rounds = max(1, max_tool_rounds)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_turn(
        self,
        messages: list[dict],
        session_id: str,
        initial_llm_response: LLMResponse,
    ) -> tuple[str, list[ToolCall]]:
        """
        Drive the tool-use loop starting from the initial LLM response.

        Args:
            messages:             The messages list that produced initial_llm_response.
                                  Extended in-place with tool_use and tool_result blocks.
            session_id:           Used for consent checks and gateway calls.
            initial_llm_response: First LLM response from orchestrator's LLM call #1.

        Returns:
            (final_response_text, list_of_all_tool_calls_executed)
            final_response_text is an empty string if the LLM returned no content
            and no tool calls were made (edge case — orchestrator handles this).
        """
        if session_id is None:
            raise ValueError("session_id must not be None")
        if initial_llm_response is None:
            raise ValueError("initial_llm_response must not be None")

        current_response = initial_llm_response
        all_tool_calls: list[ToolCall] = []
        rounds = 0

        while current_response.stop_reason == "tool_use" and rounds < self._max_tool_rounds:
            if not current_response.tool_calls:
                logger.warning(
                    "manager_agent.tool_use_no_calls",
                    extra={
                        "operation": "manager_agent.run_turn",
                        "status": "skipped",
                        "session_id": session_id,
                        "round": rounds + 1,
                    },
                )
                break

            for tool_call in current_response.tool_calls:
                tool_result = self._execute_tool(tool_call, session_id)
                all_tool_calls.append(tool_call)
                messages = self._append_tool_result(messages, tool_call, tool_result)

            rounds += 1

            start = time.time()
            current_response = self._llm.call(
                messages=messages,
                tools=self._registry.get_tool_definitions(),
                system="",
            )
            logger.info(
                "manager_agent.llm_followup",
                extra={
                    "operation": "manager_agent.run_turn",
                    "status": "success",
                    "session_id": session_id,
                    "round": rounds,
                    "latency_ms": int((time.time() - start) * 1000),
                    "model": current_response.model_used,
                },
            )

        final_text = current_response.content or ""
        return final_text, all_tool_calls

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _execute_tool(self, tool_call: ToolCall, session_id: str) -> ToolResult:
        """
        Enforce consent gate then delegate to Action Gateway.

        Returns ToolResult regardless of success or consent failure.
        Consent failures are surfaced as ToolResult(success=False, error="consent_required")
        so the LLM can prompt the user for consent in the next turn.
        """
        if self._registry.requires_consent(tool_call.tool_name):
            consent_granted = self._trust.check_consent(session_id, tool_call.tool_name)
            if not consent_granted:
                logger.warning(
                    "manager_agent.consent_denied",
                    extra={
                        "operation": "manager_agent._execute_tool",
                        "status": "skipped",
                        "tool_name": tool_call.tool_name,
                        "session_id": session_id,
                    },
                )
                return ToolResult(
                    tool_use_id=tool_call.tool_use_id,
                    tool_name=tool_call.tool_name,
                    result={},
                    success=False,
                    error="consent_required",
                )

        start = time.time()
        result = self._gateway.execute(tool_call, session_id)
        logger.info(
            "manager_agent.tool_executed",
            extra={
                "operation": "manager_agent._execute_tool",
                "status": "success" if result.success else "failure",
                "tool_name": tool_call.tool_name,
                "session_id": session_id,
                "latency_ms": int((time.time() - start) * 1000),
                "error": result.error,
            },
        )
        return result

    def _append_tool_result(
        self,
        messages: list[dict],
        tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> list[dict]:
        """
        Extend the messages list with the tool_use and tool_result blocks
        in the Anthropic message format required for multi-turn tool use.

        Anthropic format:
          - The assistant message contains the tool_use block.
          - The following user message contains the tool_result block.
        """
        # Append assistant message with the tool_use block
        messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_call.tool_use_id,
                    "name": tool_call.tool_name,
                    "input": tool_call.input_params,
                }
            ],
        })

        # Append user message with the tool_result block
        result_content: str = (
            tool_result.error
            if not tool_result.success and tool_result.error
            else str(tool_result.result)
        )
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call.tool_use_id,
                    "content": result_content,
                }
            ],
        })

        return messages
