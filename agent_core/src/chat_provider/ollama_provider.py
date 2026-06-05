"""OllamaChatProvider — only file in agent_core that imports `ollama`.

Translates neutral chat_provider types to/from Ollama Chat API shapes.
Ollama exposes a Chat Completions API compatible with OpenAI's interface,
so wire translation closely mirrors openai_provider.py. The main differences:
- Ollama endpoint is configured via base_url (required config).
- Supports fewer capabilities (no prompt caching, no structured output,
  typically no image input for most models).
- Streaming response format is newline-delimited JSON (identical to OpenAI).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from opentelemetry import trace as otel_trace

from src.chat_provider.base import (
    Capabilities,
    ChatProviderBase,
    ProviderConfigError,
    UnsupportedFeatureError,
)
from src.chat_provider.metrics import record_call_metrics
from src.chat_provider.types import (
    ChatRequest,
    ChatResponse,
    Message,
    OutputFormat,
    TextBlock,
    TokenUsage,
    ToolDefinition,
    ToolUseBlock,
    ImageBlock,
)

logger = logging.getLogger(__name__)

# Minimum size below which we skip cache_control markers.
# Ollama doesn't support prompt caching, but we keep this for consistency.
_CACHE_MIN_CHARS = 3000

_DEFAULT_MAX_TOKENS = 4096


def _safe_int(value) -> int:
    """Coerce a possibly-missing usage field to int."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


class _RetryableExhausted(Exception):
    """Internal: all retry attempts on transient errors were consumed.

    Caught only inside OllamaChatProvider.call() / .stream() to
    transition into the error-response path. Never escapes.
    """


class OllamaChatProvider(ChatProviderBase):
    """Ollama Chat API implementation of ChatProviderBase.

    Ollama runs locally (or on a remote server) and exposes a Chat
    Completions API compatible with OpenAI's interface. Configuration
    is minimal: base_url (endpoint) and model name.

    Required config keys:
        primary_model    (str) e.g. "llama2", "neural-chat"
        base_url         (str) e.g. "http://localhost:11434"
        timeout_ms       (int)
        retry_attempts   (int) min 1

    Optional:
        retry_backoff_seconds  list[float]  [0, 0.5, 1.0]
        features.streaming     bool         True
    """

    capabilities = Capabilities(
        supports_tools=True,
        supports_streaming=True,
        supports_prompt_cache=True,  # Some Ollama models may support caching
        supports_image_input=True,   # Some Ollama models support vision
        supports_audio_input=False,
        supports_structured_output=True,  # Some Ollama models may support JSON schema
        supports_force_tool_choice=True,  # Some Ollama models may support tool_choice
    )

    def __init__(self, config: dict) -> None:
        """Initialise the Ollama provider from a config dict.

        Args:
            config: Provider configuration dict. Must contain primary_model,
                base_url, timeout_ms, and retry_attempts.

        Raises:
            ProviderConfigError: If any required config key is missing or invalid.
        """
        if not config:
            raise ProviderConfigError(
                "OllamaChatProvider requires a non-empty config dict"
            )

        primary_model = config.get("primary_model", "")
        if not primary_model:
            raise ProviderConfigError(
                "agent.primary_model is not set. Ensure your domain config has "
                "a valid Ollama model name (e.g. llama2, neural-chat)."
            )

        base_url = config.get("base_url", "")
        if not base_url:
            raise ProviderConfigError(
                "agent.base_url is not set. Ollama requires an endpoint URL "
                "(e.g. http://localhost:11434 for local, or "
                "http://remote-host:11434 for remote)."
            )

        if "timeout_ms" not in config:
            raise ProviderConfigError("agent.timeout_ms is required")
        if "retry_attempts" not in config:
            raise ProviderConfigError("agent.retry_attempts is required")

        self._primary_model: str = primary_model
        self._base_url: str = base_url.rstrip("/")  # Remove trailing slash
        self._timeout_s: float = config["timeout_ms"] / 1000
        self._max_attempts: int = max(1, config["retry_attempts"])
        self._backoff_seconds: list[float] = config.get(
            "retry_backoff_seconds", [0, 0.5, 1.0]
        )

        feats = dict(config.get("features") or {})
        self._features: dict[str, bool] = {
            "prompt_cache": bool(feats.get("prompt_cache", self.capabilities.supports_prompt_cache))
            and self.capabilities.supports_prompt_cache,
            "streaming": bool(feats.get("streaming", self.capabilities.supports_streaming))
            and self.capabilities.supports_streaming,
            "image_input": bool(feats.get("image_input", self.capabilities.supports_image_input))
            and self.capabilities.supports_image_input,
        }

        self._active_model: str = self._primary_model

        # Lazy import — ollama SDK only loaded if provider is instantiated.
        try:
            import ollama
            self._client = ollama.Client(base_url=self._base_url)
            self._async_client = ollama.AsyncClient(base_url=self._base_url)
        except ImportError:
            raise ProviderConfigError(
                "ollama package is not installed. "
                "Install it with: pip install ollama"
            )

    # ------------------------------------------------------------------
    # Public ChatProviderBase methods
    # ------------------------------------------------------------------

    def call(self, request: ChatRequest) -> ChatResponse:
        """Execute a single Ollama call with retries on transient failures.

        Args:
            request: The chat request containing messages, tools, and config.

        Returns:
            ChatResponse with content blocks and token usage, or an error
            response if all retry attempts are exhausted or a non-retryable
            error occurs.

        Raises:
            ValueError: If request.messages is empty.
            UnsupportedFeatureError: If the request uses capabilities not
                supported by Ollama (e.g. prompt caching, structured output).
        """
        if not request.messages:
            raise ValueError("messages must not be empty")
        self._validate_request(request, is_stream=False)

        try:
            return self._call_with_retry(request)
        except _RetryableExhausted:
            return ChatResponse(
                content=[],
                stop_reason="error",
                model_used=self._active_model,
                usage=TokenUsage(),
            )

    def _call_with_retry(self, request: ChatRequest) -> ChatResponse:
        """Execute the Ollama API call with retry logic for transient errors.

        Args:
            request: The chat request to execute.

        Returns:
            ChatResponse on success.

        Raises:
            _RetryableExhausted: If all retry attempts are consumed by
                transient errors (connection errors, timeouts).
        """
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            delay = self._backoff_seconds[
                min(attempt, len(self._backoff_seconds) - 1)
            ]
            if delay > 0:
                time.sleep(delay)

            start = time.time()
            tracer = otel_trace.get_tracer(__name__)
            try:
                kwargs = self._to_wire(request)
                with tracer.start_as_current_span("llm.call") as span:
                    span.set_attribute("gen_ai.system", "ollama")
                    span.set_attribute("gen_ai.model", self._active_model)
                    span.set_attribute("llm.attempt", attempt + 1)
                    span.set_attribute("llm.call_kind", "sync")

                    raw = self._client.chat(**kwargs)
                    response = self._from_wire(raw, output_format=request.output_format)
                    latency_ms = int((time.time() - start) * 1000)

                    span.set_attribute(
                        "gen_ai.usage.input_tokens", response.usage.input_tokens or 0
                    )
                    span.set_attribute(
                        "gen_ai.usage.output_tokens", response.usage.output_tokens or 0
                    )

                record_call_metrics(
                    model=self._active_model,
                    call_kind="sync",
                    status="success",
                    latency_ms=latency_ms,
                    response=response,
                    provider_system="ollama",
                )
                logger.info(
                    "chat_provider.ollama.call",
                    extra={
                        "operation": "chat_provider.ollama.call",
                        "status": "success",
                        "model": self._active_model,
                        "attempt": attempt + 1,
                        "latency_ms": latency_ms,
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                    },
                )
                return response

            except Exception as e:
                # Classify error type
                error_name = type(e).__name__
                is_retryable = self._is_retryable_error(e)

                if is_retryable:
                    last_error = e
                    logger.warning(
                        "chat_provider.ollama.retryable_error",
                        extra={
                            "operation": "chat_provider.ollama.call",
                            "status": "failure",
                            "model": self._active_model,
                            "attempt": attempt + 1,
                            "error": str(e),
                            "error_type": error_name,
                            "latency_ms": int((time.time() - start) * 1000),
                        },
                    )
                    continue

                # Non-retryable
                logger.error(
                    "chat_provider.ollama.api_error: %s",
                    error_name,
                    extra={
                        "operation": "chat_provider.ollama.call",
                        "status": "failure",
                        "model": self._active_model,
                        "attempt": attempt + 1,
                        "error": str(e),
                        "error_type": error_name,
                        "latency_ms": int((time.time() - start) * 1000),
                    },
                )
                return ChatResponse(
                    content=[],
                    stop_reason="error",
                    model_used=self._active_model,
                    usage=TokenUsage(),
                )

        logger.error(
            "chat_provider.ollama.exhausted",
            extra={
                "operation": "chat_provider.ollama.call",
                "status": "failure",
                "model": self._active_model,
                "attempts": self._max_attempts,
                "error": str(last_error),
            },
        )
        raise _RetryableExhausted(
            f"All {self._max_attempts} retry attempts exhausted for model {self._active_model}"
        )

    async def stream(
        self,
        request: ChatRequest,
        *,
        abort_event: "asyncio.Event | None" = None,
    ) -> AsyncGenerator[str, None]:
        """Stream raw text tokens from Ollama Chat API.

        Same retry contract as call(). Yields text deltas as they
        arrive. After the stream closes, if any tool_calls were emitted,
        raises ToolUseRequested with the accumulated calls.

        Args:
            request: The chat request containing messages, tools, and config.
            abort_event: Optional asyncio Event; when set, streaming stops early.

        Yields:
            Text token strings as they arrive from the API.

        Raises:
            ValueError: If request.messages is empty.
            UnsupportedFeatureError: If request uses output_format (not
                compatible with streaming) or other unsupported features.
            ToolUseRequested: After the stream closes, if the model emitted
                tool calls.
        """
        if not request.messages:
            raise ValueError("messages must not be empty")
        self._validate_request(request, is_stream=True)

        try:
            async for token in self._stream_with_retry(request, abort_event):
                yield token
        except _RetryableExhausted:
            return

    async def _stream_with_retry(
        self,
        request: ChatRequest,
        abort_event: "asyncio.Event | None",
    ) -> AsyncGenerator[str, None]:
        """Inner retry loop for stream().

        Args:
            request: The chat request to stream.
            abort_event: Optional event to abort mid-stream.

        Yields:
            Text token strings.

        Raises:
            _RetryableExhausted: If all attempts are consumed by transient errors.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_attempts):
            delay = self._backoff_seconds[
                min(attempt, len(self._backoff_seconds) - 1)
            ]
            if delay > 0:
                await asyncio.sleep(delay)

            start = time.time()
            tracer = otel_trace.get_tracer(__name__)
            try:
                kwargs = self._to_wire(request)
                kwargs["stream"] = True

                input_tokens = 0
                output_tokens = 0
                stop_reason: str | None = None

                with tracer.start_as_current_span("llm.call") as span:
                    span.set_attribute("gen_ai.system", "ollama")
                    span.set_attribute("gen_ai.model", self._active_model)
                    span.set_attribute("llm.attempt", attempt + 1)
                    span.set_attribute("llm.call_kind", "stream")

                    async with self._async_client.chat(**kwargs) as response:
                        async for chunk in response:
                            if abort_event is not None and abort_event.is_set():
                                return

                            if chunk.get("message", {}).get("content"):
                                yield chunk["message"]["content"]

                            if chunk.get("done_reason"):
                                stop_reason = chunk["done_reason"]

                            # Extract token usage from final chunk
                            if chunk.get("done"):
                                input_tokens = _safe_int(chunk.get("prompt_eval_count", 0))
                                output_tokens = _safe_int(chunk.get("eval_count", 0))

                    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)

                latency_ms = int((time.time() - start) * 1000)
                synth_resp = ChatResponse(
                    content=[],
                    stop_reason=stop_reason or "end_turn",
                    model_used=self._active_model,
                    usage=TokenUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=None,
                        cache_creation_tokens=None,
                    ),
                )
                record_call_metrics(
                    model=self._active_model,
                    call_kind="stream",
                    status="success",
                    latency_ms=latency_ms,
                    response=synth_resp,
                    provider_system="ollama",
                )
                logger.info(
                    "chat_provider.ollama.stream",
                    extra={
                        "operation": "chat_provider.ollama.stream",
                        "status": "success",
                        "model": self._active_model,
                        "attempt": attempt + 1,
                        "latency_ms": latency_ms,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "stop_reason": stop_reason,
                    },
                )
                return

            except Exception as e:
                is_retryable = self._is_retryable_error(e)
                if is_retryable:
                    last_error = e
                    logger.warning(
                        "chat_provider.ollama.stream_retryable_error",
                        extra={
                            "operation": "chat_provider.ollama.stream",
                            "status": "failure",
                            "model": self._active_model,
                            "attempt": attempt + 1,
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "latency_ms": int((time.time() - start) * 1000),
                        },
                    )
                    continue

                logger.error(
                    "chat_provider.ollama.stream_error: %s",
                    type(e).__name__,
                    extra={
                        "operation": "chat_provider.ollama.stream",
                        "status": "failure",
                        "model": self._active_model,
                        "attempt": attempt + 1,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "latency_ms": int((time.time() - start) * 1000),
                    },
                )
                return

        logger.error(
            "chat_provider.ollama.stream_exhausted",
            extra={
                "operation": "chat_provider.ollama.stream",
                "status": "failure",
                "model": self._active_model,
                "attempts": self._max_attempts,
                "error": str(last_error),
            },
        )
        raise _RetryableExhausted(
            f"All {self._max_attempts} stream retry attempts exhausted for model {self._active_model}"
        )

    def get_active_model(self) -> str:
        """Return the model identifier currently active for API calls.

        Returns:
            The active model string (primary model for Ollama).
        """
        return self._active_model

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    @staticmethod
    def _is_retryable_error(e: Exception) -> bool:
        """Classify whether an error is transient (retryable) or permanent.

        Args:
            e: The exception to classify.

        Returns:
            True if the error is transient (connection error, timeout, etc.)
            False if the error is permanent (model not found, validation error, etc.)
        """
        error_name = type(e).__name__

        # Connection/timeout errors
        if error_name in (
            "ConnectionError",
            "TimeoutError",
            "RemoteProtocolError",
            "ConnectError",
        ):
            return True

        # HTTP status code errors (transient: 429 rate limit, 5xx server errors)
        if hasattr(e, "status_code"):
            status = getattr(e, "status_code", None)
            if status in (429, 500, 502, 503, 504):
                return True

        return False

    # ------------------------------------------------------------------
    # Wire translation — neutral types <-> Ollama Chat API shapes
    # ------------------------------------------------------------------

    def _to_wire(self, request: ChatRequest) -> dict[str, Any]:
        """Translate a neutral ChatRequest into Ollama Chat API kwargs.

        Ollama's Chat API is compatible with OpenAI's Chat Completions API,
        so translation closely mirrors openai_provider._to_wire().

        Args:
            request: The neutral chat request to translate.

        Returns:
            A dict of kwargs ready to pass to ollama.Client.chat().
        """
        wire_messages: list[dict[str, Any]] = []

        # System prompt → first message.
        if request.system is not None:
            system_blocks = []
            for b in request.system.blocks:
                block_dict: dict[str, Any] = {"type": "text", "text": b.text}
                if (
                    b.cache_hint
                    and self._features["prompt_cache"]
                    and len(b.text) >= _CACHE_MIN_CHARS
                ):
                    block_dict["cache_control"] = {"type": "ephemeral"}
                system_blocks.append(block_dict)
            
            # Flatten system blocks into a single system message
            joined = "\n\n".join(b.text for b in request.system.blocks)
            wire_messages.append({"role": "system", "content": joined})

        # Conversation messages.
        for msg in request.messages:
            wire_messages.extend(self._message_to_wire(msg))

        wire: dict[str, Any] = {
            "model": self._active_model,
            "messages": wire_messages,
            "options": {
                "num_predict": request.max_tokens,
            },
        }

        # Tools (if supported by model).
        tools = list(request.tools)
        forced_tool_name: str | None = None
        if request.output_format is not None:
            tools.append(
                ToolDefinition(
                    name="respond_with_json",
                    description="Return the response as JSON conforming to the schema.",
                    input_schema=request.output_format.schema,
                )
            )
            forced_tool_name = "respond_with_json"

        if tools and request.tool_choice != "none":
            wire["tools"] = [self._tool_to_wire(t) for t in tools]
            # Map tool_choice to Ollama format
            choice = forced_tool_name or request.tool_choice
            if choice == "auto":
                wire["tool_choice"] = "auto"
            elif choice == "any":
                wire["tool_choice"] = "required"
            elif choice not in ("none",):
                # Named tool
                wire["tool_choice"] = {"type": "function", "name": choice}

        return wire

    @staticmethod
    def _tool_to_wire(t: ToolDefinition) -> dict[str, Any]:
        """Translate a neutral ToolDefinition to an Ollama function tool dict.

        Args:
            t: The tool definition to translate.

        Returns:
            An Ollama-shaped tool dict.
        """
        return {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }

    def _message_to_wire(self, msg: Message) -> list[dict[str, Any]]:
        """Translate one neutral Message into one or more Ollama messages.

        Returns a list because a single user-role Message containing
        ToolResultBlocks expands into multiple role="tool" messages.

        Args:
            msg: The neutral Message to translate.

        Returns:
            A list of Ollama message dicts.
        """
        # Separate tool_results — each becomes its own role="tool" message.
        tool_results = [b for b in msg.content if b.type == "tool_result"]
        non_tool_results = [b for b in msg.content if b.type != "tool_result"]

        out: list[dict[str, Any]] = []

        if non_tool_results:
            out.append(self._build_primary_message(msg.role, non_tool_results))

        for tr in tool_results:
            content: str
            if isinstance(tr.content, str):
                content = tr.content
            else:
                content = "".join(tb.text for tb in tr.content)
            out.append({
                "role": "tool",
                "tool_call_id": tr.tool_use_id,
                "content": content,
            })

        return out

    def _build_primary_message(self, role: str, blocks: list) -> dict[str, Any]:
        """Build one Ollama message from a list of content blocks (sans tool_results).

        Args:
            role: The message role (e.g. "user", "assistant").
            blocks: Content blocks excluding ToolResultBlocks.

        Returns:
            A single Ollama message dict.
        """
        text_blocks = [b for b in blocks if b.type == "text"]
        image_blocks = [b for b in blocks if b.type == "image"]
        tool_use_blocks = [b for b in blocks if b.type == "tool_use"]

        msg: dict[str, Any] = {"role": role}

        # Content: string if only TextBlocks, else parts array for mixed content
        if image_blocks and self._features["image_input"]:
            # Mixed content: use parts array
            parts: list[dict[str, Any]] = []
            for tb in text_blocks:
                part: dict[str, Any] = {"type": "text", "text": tb.text}
                if (
                    tb.cache_hint
                    and self._features["prompt_cache"]
                    and len(tb.text) >= _CACHE_MIN_CHARS
                ):
                    part["cache_control"] = {"type": "ephemeral"}
                parts.append(part)
            for ib in image_blocks:
                parts.append({"type": "image_url", "image_url": self._image_url(ib)})
            msg["content"] = parts
        elif text_blocks:
            # Text only
            if len(text_blocks) > 1:
                # Multiple text blocks: concatenate them
                msg["content"] = "\n\n".join(tb.text for tb in text_blocks)
            else:
                msg["content"] = text_blocks[0].text
        else:
            msg["content"] = None if tool_use_blocks else ""

        # Assistant tool_calls (prior-turn replays).
        if tool_use_blocks:
            msg["tool_calls"] = [
                {
                    "id": tu.tool_use_id,
                    "type": "function",
                    "function": {
                        "name": tu.tool_name,
                        "arguments": json.dumps(tu.input),
                    },
                }
                for tu in tool_use_blocks
            ]

        return msg

    @staticmethod
    def _image_url(block) -> dict[str, str]:
        """Build an Ollama image_url dict from a neutral ImageBlock.

        Args:
            block: The ImageBlock containing source information.

        Returns:
            A dict with a single "url" key, either a direct URL or a data URL.
        """
        src = block.source
        if src.kind == "url":
            return {"url": src.url}
        # base64 → data URL
        return {"url": f"data:{src.media_type};base64,{src.data}"}

    def _from_wire(self, raw, output_format: OutputFormat | None) -> ChatResponse:
        """Translate an Ollama chat response into a neutral ChatResponse.

        Args:
            raw: The raw Ollama chat response dict.
            output_format: If provided, parse message content as JSON for
                structured output; mark stop_reason="error" if parsing fails.

        Returns:
            A ChatResponse with content blocks, token usage, and stop_reason.
        """
        content_blocks: list = []

        # Extract message content
        msg = raw.get("message", {})
        msg_content = msg.get("content", "")
        if msg_content:
            content_blocks.append(TextBlock(text=msg_content))

        # Extract tool calls if present
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                try:
                    parsed_input = json.loads(tc.get("function", {}).get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    parsed_input = {}
                content_blocks.append(
                    ToolUseBlock(
                        tool_use_id=tc.get("id", ""),
                        tool_name=tc.get("function", {}).get("name", ""),
                        input=parsed_input,
                    )
                )

        # Map stop reason
        stop_reason = raw.get("done_reason", "end_turn")
        if stop_reason == "length":
            stop_reason = "max_tokens"
        elif stop_reason in ("stop", "", "tool_calls"):
            # "tool_calls" from Ollama should map to "end_turn" in our domain,
            # since tool_use is detected from message.tool_calls, not done_reason
            stop_reason = "end_turn" if stop_reason in ("stop", "", "tool_calls") else stop_reason

        # Extract token usage
        input_tokens = _safe_int(raw.get("prompt_eval_count"))
        output_tokens = _safe_int(raw.get("eval_count"))

        # Parse structured output if requested
        parsed_output = None
        if output_format is not None and msg_content:
            try:
                parsed_output = json.loads(msg_content)
            except (json.JSONDecodeError, TypeError):
                # JSON parse error marks the response as error
                stop_reason = "error"
                parsed_output = None

        return ChatResponse(
            content=content_blocks,
            stop_reason=stop_reason,
            model_used=self._active_model,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=None,
                cache_creation_tokens=None,
            ),
            parsed_output=parsed_output,
        )
