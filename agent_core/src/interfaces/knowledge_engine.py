"""
agent_core/interfaces/knowledge_engine.py

Contract that Agent Core requires from the Knowledge Engine DPG.
The Knowledge Engine assembles the full prompt (persona + RAG + glossary + history)
before every LLM call. Agent Core provides context; Knowledge Engine returns messages.
"""

from abc import ABC, abstractmethod

from src.models import SessionState


class KnowledgeEngineBase(ABC):

    @abstractmethod
    def assemble_prompt(
        self,
        session_id: str,
        user_message: str,
        session_state: SessionState,
    ) -> list[dict]:
        """
        Build and return the complete messages list for the LLM call.

        Format:
            [{"role": "user" | "assistant", "content": str}, ...]

        The system prompt (persona + RAG context + glossary mappings) is embedded
        in the first message. Conversation history is sourced from session_state.history.

        Returns an empty list only if user_message is empty — never raises.
        """
