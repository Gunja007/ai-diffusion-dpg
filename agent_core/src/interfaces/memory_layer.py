"""
agent_core/interfaces/memory_layer.py

Contract that Agent Core requires from the Memory Layer DPG.
The Memory Layer implementation (in its own repo/folder) must inherit this base class.
"""

from abc import ABC, abstractmethod

from src.models import SessionState


class MemoryLayerBase(ABC):

    @abstractmethod
    def read_session(self, session_id: str) -> SessionState:
        """
        Load session state for the given session.
        Returns SessionState.empty(session_id) if no prior state exists.
        Never raises for a missing session.
        """

    @abstractmethod
    def write_session(self, session_id: str, state: SessionState) -> None:
        """
        Persist session state. Overwrites any existing state for the session.
        Called asynchronously after the response is delivered — must not block.
        """

    @abstractmethod
    def get_user_profile(self, session_id: str) -> dict:
        """
        Return persistent user profile data.
        Returns an empty dict if no profile exists for the session.
        """

    @abstractmethod
    def clear_session(self, session_id: str) -> None:
        """Delete all session-scoped state for the given session_id."""
