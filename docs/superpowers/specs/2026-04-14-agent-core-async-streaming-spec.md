# Agent Core: Async Orchestrator + SSE Streaming — Spec

**Status:** Approved for implementation
**Date:** 2026-04-14
**Issue:** [#71](https://github.com/sanketika-labs/ai-diffusion-dpg/issues/71)
**Sub-tasks:** [#74](https://github.com/sanketika-labs/ai-diffusion-dpg/issues/74) [#75](https://github.com/sanketika-labs/ai-diffusion-dpg/issues/75) [#76](https://github.com/sanketika-labs/ai-diffusion-dpg/issues/76) [#77](https://github.com/sanketika-labs/ai-diffusion-dpg/issues/77)
**Reopened:** [#78](https://github.com/sanketika-labs/ai-diffusion-dpg/issues/78) — `POST /stream_turn` endpoint added as a dev/test tool and direct-invocation path; not a production reach layer path (reach layers in `assembly_mode: direct` use `POST /process_turn` instead)
**Depends on:** None — standalone change.
**Blocks:** TurnAssembler spec (#72), Reach Layer restructuring spec (#73)

---

## Problem

Agent Core's `process_turn()` is fully synchronous. For voice channels, end-to-end latency from VAD end-of-speech to first TTS audio is 3–6 seconds. The 2s target requires sentence-by-sentence streaming so TTS can begin sentence 1 while the LLM is still generating sentence 2.

Additionally, there is no mid-turn visibility — callers and UIs receive no signal for what Agent Core is doing (reading memory, checking trust, invoking tools, etc.).

---

## Architecture

### Two separate invocation paths

`process_turn()` is **left entirely unchanged** — it remains synchronous and continues to be the only method the `POST /process_turn` endpoint calls. Dev tooling and direct testing use it as-is.

`stream_turn()` is a **new async method** added alongside `process_turn()`. It runs the same 13-step pipeline but:
- Calls async variants of all HTTP clients (separate instances from the sync ones).
- Yields `StreamEvent`s as the pipeline progresses.
- Called in-process by `TurnAssembler._invoke()` when a session-mode reach layer triggers a turn.
- Also exposed directly via `POST /stream_turn` for dev tooling, test harnesses, and any caller that wants raw streaming without session management.

Both methods are injected with their respective clients at startup. The sync and async client instances are independent — no shared state.

```
POST /process_turn             →  agent_core.process_turn()    →  sync HTTP clients  →  TurnResult JSON
POST /stream_turn              →  agent_core.stream_turn()     →  async HTTP clients →  SSE (StreamEvents)
TurnAssembler._invoke()        →  agent_core.stream_turn()     →  async HTTP clients →  SSE via event queue
```

`POST /stream_turn` and `TurnAssembler._invoke()` are **independent** — `stream_turn()` has no knowledge of TurnAssembler. Agent Core starts up fully functional with just `/process_turn` and `/stream_turn`. TurnAssembler is wired separately and depends on `stream_turn()`; the reverse is not true.

### Async HTTP clients

A new parallel set of async HTTP client classes is added alongside the existing sync ones:

| Sync (existing, unchanged) | Async (new) |
|---|---|
| `TrustLayerHttpClient` | `AsyncTrustLayerHttpClient` |
| `MemoryLayerHttpClient` | `AsyncMemoryLayerHttpClient` |
| `KnowledgeEngineHttpClient` | `AsyncKnowledgeEngineHttpClient` |
| `ActionGatewayHttpClient` | `AsyncActionGatewayHttpClient` |
| `ObservabilityLayerHttpClient` | `AsyncObservabilityLayerHttpClient` |

Each async client has the same method signatures as its sync counterpart but uses `httpx.AsyncClient` and `await`. The abstract interface ABCs in `agent_core/src/interfaces/` are **not changed** — new async ABCs are added alongside them:

| Sync interface (unchanged) | Async interface (new) |
|---|---|
| `TrustLayerBase` | `AsyncTrustLayerBase` |
| `MemoryLayerBase` | `AsyncMemoryLayerBase` |
| `KnowledgeEngineBase` | `AsyncKnowledgeEngineBase` |
| `ActionGatewayBase` | `AsyncActionGatewayBase` |
| `ObservabilityLayerBase` | `AsyncObservabilityLayerBase` |

`AgentCore.__init__()` gains five new optional parameters (`async_memory`, `async_trust`, etc.). `stream_turn()` uses these; `process_turn()` continues to use the original sync ones.

### Event model

Two categories of events are introduced. Both are yielded by `stream_turn()`.

**SignalEvent** — pipeline stage notification. No trust check applied.

```python
@dataclass
class SignalEvent:
    type: str = "signal"
    stage: str = ""    # see valid stages below
    status: str = ""   # "start" | "complete" | "skipped"
    detail: str = ""   # optional human-readable info
```

Valid stages: `memory_read`, `trust_input`, `nlu`, `routing`, `ke_retrieval`, `tool_start`, `tool_end`, `trust_output`, `memory_write`

Each stage emits two events: `status="start"` before the operation and `status="complete"` (or `"skipped"`) after.

**SentenceEvent** — one trust-checked sentence from the LLM response.

```python
@dataclass
class SentenceEvent:
    type: str = "sentence"
    text: str = ""
    sentence_index: int = 0
```

**DoneEvent** — terminal event. Always the last event in a stream.

```python
@dataclass
class DoneEvent:
    type: str = "done"
    was_escalated: bool = False
    was_tool_used: bool = False
    model_used: str = ""
    latency_ms: int = 0
    turn_id: str = ""
    turn_status: str = "completed"  # "completed" | "interrupted" | "abandoned"

StreamEvent = SignalEvent | SentenceEvent | DoneEvent
```

Each event serialises to `data: <json>\n\n` for SSE delivery.

### stream_call() — LLM wrapper

Add to `ClaudeLLMWrapper` and `LLMWrapperBase`:

```python
async def stream_call(
    self,
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
    model_override: str | None = None,
) -> AsyncGenerator[str, None]
```

Uses `anthropic.messages.stream()`. Same retry + fallback model logic as `call()`. Same timeout enforcement and structured logging (emitted on stream close with token counts).

If `tool_use` stop reason is detected mid-stream: collect accumulated tool call blocks, raise `ToolUseRequested(tool_calls)` so `stream_turn()` can handle the tool loop and resume.

### stream_turn() — orchestrator

```python
async def stream_turn(self, turn_input: TurnInput) -> AsyncGenerator[StreamEvent, None]
```

Runs the same 13-step pipeline as `process_turn()` but yields events as it progresses. Uses **async HTTP clients only** (`self._async_memory`, `self._async_trust`, etc.).

**Steps 1–7 (pre-LLM):** yield `SignalEvent(stage=..., status="start")` before and `SignalEvent(stage=..., status="complete")` after each stage.

**Step 8 (LLM streaming):** call `llm.stream_call()`. Accumulate tokens into sentences by splitting on `.`, `?`, `!`, `।` (Devanagari danda U+0964), `?` (full-width). For each complete sentence:
1. Call `async_trust.check_output(sentence_text)`
2. If allowed → yield `SentenceEvent(text=sentence_text, sentence_index=n)`
3. If blocked → yield `SentenceEvent(text=fallback_phrase, sentence_index=n)`, set `was_escalated=True`

Trust check applies **only** to `SentenceEvent` (LLM output). `SignalEvent`s bypass trust entirely.

**Step 9 (tool use):** if `ToolUseRequested` is raised: yield `SignalEvent(stage="tool_start")`, execute via async Action Gateway, yield `SignalEvent(stage="tool_end")`, resume LLM streaming with tool result.

**Final:** yield `DoneEvent` with aggregated metadata.

Steps 12–13 (async memory write + observability emit) fire after `DoneEvent` is yielded via `asyncio.create_task()`. If `stream_turn()` is cancelled before the memory write step, the async write task is also cancelled — no stale writes occur.

**One new HTTP endpoint is added: `POST /stream_turn`.**

```
POST /stream_turn
Content-Type: application/json
Body: { session_id, user_message, channel, timestamp_ms, user_id?, fresh? }

Response: text/event-stream
data: {"type":"signal","stage":"memory_read","status":"start",...}\n\n
data: {"type":"sentence","text":"...","sentence_index":0}\n\n
data: {"type":"done","was_escalated":false,...}\n\n
```

Returns `text/event-stream`. Connection closes after `DoneEvent` is sent. No session buffer, no TurnAssembler — one-shot streaming call.

Reach layers in `assembly_mode: session` use `POST /sessions/{id}/input` (TurnAssembler path, spec #72). `POST /stream_turn` is for dev tooling, test harnesses, and any caller that wants raw streaming without session management.

---

## Files changed

| File | Change |
|---|---|
| `agent_core/src/models.py` | Add `SignalEvent`, `SentenceEvent`, `DoneEvent`, `StreamEvent`, `ToolUseRequested` |
| `agent_core/src/llm_wrapper/base.py` | Add `stream_call()` abstract method |
| `agent_core/src/llm_wrapper/claude_wrapper.py` | Implement `stream_call()` with `anthropic.messages.stream()` |
| `agent_core/src/interfaces/async_*.py` | New async ABC interfaces for all 5 downstream services |
| `agent_core/src/http_clients/async_*.py` | New async HTTP client implementations |
| `agent_core/src/base.py` | Add `stream_turn()` abstract method |
| `agent_core/src/orchestrator.py` | Add `stream_turn()`; accept async client params in `__init__`; `process_turn()` unchanged |
| `agent_core/src/servers/orchestration_server.py` | Add `POST /stream_turn` SSE endpoint; `POST /process_turn` unchanged |

---

## Key constraints

- `process_turn()` and `POST /process_turn` are **completely unchanged** — no migration, no risk to existing behaviour.
- Sync HTTP clients are untouched. Async clients are new additions.
- Trust check under streaming: per sentence, not batched. Batching reintroduces the latency being eliminated.
- Trust infra failure during streaming: treat as "allow", log error, never block the stream.
- Sentence splitter handles ASCII and Devanagari boundaries.
- Coverage ≥ 70% maintained across `agent_core/`.

---

## Execution order

`#74` (event models + `stream_call()`) must merge before `#75`, `#76`, `#77` can begin. The remaining three (`#75` async clients, `#76` `stream_turn()`, `#77` `DoneEvent` + memory consistency) are independent of each other once `#74` is in.
