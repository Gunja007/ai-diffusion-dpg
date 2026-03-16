"""
agent_core/exceptions.py

Typed exceptions raised within Agent Core.
Callers catch specific types — no bare Exception handling anywhere in agent_core/.
"""


class AgentCoreError(Exception):
    """Base exception for all Agent Core failures."""


class LLMCallError(AgentCoreError):
    """All retry attempts on the primary model exhausted without a response."""


class LLMFallbackError(AgentCoreError):
    """Fallback model also failed after primary model exhaustion."""


class TrustViolationError(AgentCoreError):
    """Trust Layer blocked execution — input or output did not pass safety check."""


class ToolExecutionError(AgentCoreError):
    """Action Gateway returned a failure result for a tool call."""


class ConsentRequiredError(AgentCoreError):
    """Write or identity connector was called without confirmed user consent."""


class ConfigurationError(AgentCoreError):
    """Invalid, missing, or inconsistent domain configuration detected at startup."""
