"""
agent_core/interfaces/trust_layer.py

Contract that Agent Core requires from the Trust Layer DPG.
check_input() and check_output() are both mandatory on every turn.
Neither check may be skipped — this is enforced in orchestrator.py.
"""

from abc import ABC, abstractmethod

from src.models import TrustCheckResult


class TrustLayerBase(ABC):

    @abstractmethod
    def check_input(self, session_id: str, user_message: str) -> TrustCheckResult:
        """
        Evaluate raw user input against content rules and topic firewall.
        Must be called before any LLM call.
        Returns TrustCheckResult with action "allow", "block", or "escalate".
        """

    @abstractmethod
    def check_output(self, session_id: str, llm_response: str) -> TrustCheckResult:
        """
        Evaluate LLM-generated response against output safety rules.
        Must be called before delivering any response to the user.
        Returns TrustCheckResult with action "allow", "block", or "escalate".
        """

    @abstractmethod
    def check_consent(self, session_id: str, connector_name: str) -> bool:
        """
        Verify that confirmed user consent exists for a write or identity connector.
        Returns True if consent is on record, False otherwise.
        Called by ManagerAgent before executing any write/identity tool call.
        """
