"""
agent_core/interfaces/learning_layer.py

Contract that Agent Core requires from the Learning Layer DPG.
emit_turn() is always called asynchronously — after the response is delivered.
Implementations must be non-blocking. Any blocking I/O must be done in a
background thread or queue inside the implementation, not in this call.
"""

from abc import ABC, abstractmethod

from src.models import TurnEvent


class LearningLayerBase(ABC):

    @abstractmethod
    def emit_turn(self, event: TurnEvent) -> None:
        """
        Record turn-level observability data for audit, quality evaluation,
        and feedback signal collection.

        Contract:
        - Must not block the caller.
        - Must not raise — swallow and log any internal errors internally.
        - Called from a daemon thread inside orchestrator._post_turn().
        """
