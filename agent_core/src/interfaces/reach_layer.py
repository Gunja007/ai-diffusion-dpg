"""
agent_core/interfaces/reach_layer.py

Contract that Agent Core requires from the Reach Layer DPG.
In PoC this is a CLI stub. In production it is a channel adapter
(WhatsApp, Web, VOIP, Mobile SDK).
"""

from abc import ABC, abstractmethod

from src.models import TurnInput, TurnResult


class ReachLayerBase(ABC):

    @abstractmethod
    def receive(self) -> TurnInput:
        """
        Block until a user message is available on the channel.
        Returns a normalised TurnInput regardless of the source channel.
        """

    @abstractmethod
    def deliver(self, result: TurnResult) -> None:
        """
        Send the agent response back to the user on their channel.
        Called after process_turn() returns — the response is ready for delivery.
        """
