"""
agent_core/llm_wrapper/base.py

Abstract interface for the LLM Inferencing Wrapper.
Defines the contract that all LLM provider implementations must satisfy.
Agent Core and ManagerAgent depend only on this interface — never on a concrete class.
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.models import LLMResponse


class LLMWrapperBase(ABC):

    @abstractmethod
    def call(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        """
        Execute a single LLM call.

        Args:
            messages:       Conversation messages in Anthropic format.
            tools:          Tool definitions to inject. Pass empty list if no tools.
            system:         System prompt string.
            model_override: If provided, use this model instead of the active model.
                            Used internally for fallback switching.

        Returns:
            LLMResponse — always. Never raises.
            On failure, returns LLMResponse with stop_reason="error" and content=None.
        """

    @abstractmethod
    def get_active_model(self) -> str:
        """
        Return the name of the currently active model.
        Reflects primary model under normal conditions;
        reflects fallback model after a primary exhaustion event.
        """
