# CLAUDE.md — AI Composition Framework Development Reference

This file is the persistent development context for AI-assisted coding on the DPG Composition Framework.
Read this before writing any code. Follow every rule in Section 6 without exception.

---

## 1. Project Overview

This project is a **composition framework for building AI-powered voice and chat systems** across any domain
(government services, enterprise, public institutions). Instead of building AI from scratch, deployers
assemble a system from **7 standardised DPG building blocks** and configure it for their domain using a
**Domain Configuration Kit** (YAML).

The framework separates:
- **What stays the same** across deployments — the 7 DPG blocks and their runtime behaviour
- **What changes** per deployment — domain knowledge, connectors, persona, safety policy (all in YAML config)

---

## 2. Core Architecture

The system is a **modular composition framework**. The 7 DPG blocks are reusable, stateless runtime
components. All domain-specific intelligence lives outside them in the Domain Configuration Kit.

Functional groupings:

| Group | Blocks |
|---|---|
| Intelligence & Integration | Knowledge Engine, Action Gateway |
| Orchestration & Trust | Agent Core, Trust Layer |
| State & Memory | Memory Layer |
| Channels & Reach | Reach Layer |
| Learning & Observability | Learning Layer |

The **Domain Configuration Kit** configures all 7 blocks. The **Agent Core** orchestrates all of them at
runtime.

---

## 3. The 7 DPG Modules

### Knowledge Engine
Assembles the complete prompt for each LLM call. Receives NLU results from Agent Core as
parameters — it does not run Language Normalisation or NLU itself.
- Maps entity values to canonical concepts (Glossary & Domain Vocabulary)
- Performs semantic RAG retrieval over ingested domain documents (Static Knowledge Base)
- Processes non-text inputs: images, PDFs (Multimodal Input Handler)
- Receives session history from Agent Core in the request body (does not call Memory Layer directly)

### Memory Layer
Manages all state across three scopes:
- **Turn** — raw input, intermediate results for the current processing cycle only
- **Session** — turn history, confirmed entities, workflow step within one conversation
- **Persistent** — user profile, journey history, outcomes across sessions

Internal components: Session Memory Handler, User Profile Store, Task State Manager,
Broadcast/Incident State.

### Trust Layer
Mandatory safety and compliance gate. **Every input and every output passes through this layer.**
- **Content rules** — blocks harmful inputs, enforces topic boundaries
- **Output rules** — verifies LLM response before delivery
- **Consent rules** — manages user consent flows (DPDP Act compliance)
- **Escalation rules** — detects when AI must hand off to a human
- **Topic firewall** — certain topics always route to humans regardless of AI confidence

Internal components: Guardrails Engine, Consent & Compliance Handler, HITL Handler,
Priority & Escalation Classifier.

### Agent Core (Orchestrator)
The central intelligence and orchestration layer. **The only component that calls the LLM.**
- Runs Language Normalisation (dialect detection, code-switching, transliteration) before calling KE
- Runs NLU Processor (intent classification, entity extraction, sentiment) before calling KE
- Coordinates the execution sequence of all other DPGs on every turn
- Owns the tool execution loop (LLM → tool → LLM)
- Manages LLM retry and fallback model switching
- Runtime behaviour is fully driven by the Domain Configuration Kit (YAML)

Internal components: LLM Inferencing Wrapper, Language Normaliser, NLU Processor,
Manager Agent (intent-based routing), Orchestration Config Layer.

### Action Gateway
The framework's only interface with the external world.
- Executes all external API calls expressed as LLM tool calls
- The LLM never touches external systems directly — it expresses intent via tool definitions
- Returns normalised results to the Agent Core to be fed back into the conversation
- Write connectors always require explicit user consent (enforced via Trust Layer)

Connector types: Read (query), Write (action), Identity (verification).

### Reach Layer
Manages inbound and outbound communication channels.
- Normalises input across VOIP, WhatsApp, Web, Mobile SDK (Channel Adapter)
- Manages outbound campaigns and re-engagement (Campaign Orchestrator)
- Preserves full context on cross-channel transitions (Handoff Manager)

### Learning Layer
Asynchronous observability layer. **Runs entirely out-of-band — never in the response path.**
- Audit log — every prompt, response, routing decision, consent event (timestamped)
- Quality evaluation — groundedness, relevance, hallucination detection scores
- Signal collection — explicit and implicit feedback, structured as training data
- Outcome tracking — resolved/pending/action-taken after each call

Internal components: LLM Observability, Evals Framework, Feedback Signal Collector, Outcome Tracker.

---

## 4. Runtime Interaction Flow

Every user turn follows this exact sequence:

```
Input received (Reach Layer)
  → Agent Core: read state (Memory Layer)
  → Agent Core: safety check on input (Trust Layer)
  → Agent Core: Language Normalisation (internal — dialect, code-switching, transliteration)
  → Agent Core: NLU Processor (internal — intent, entities, sentiment) → early exit if unknown/low-confidence
  → Agent Core: assemble prompt (Knowledge Engine — receives NLU results in request body)
  → Agent Core: LLM call #1
  → [if tool_use] Agent Core: execute tool (Action Gateway) → LLM call #2
  → Agent Core: safety check on output (Trust Layer)
  → Agent Core: deliver response (Reach Layer)
  → [async] Agent Core: write state (Memory Layer)
  → [async] Agent Core: emit turn events (Learning Layer)
```

**Hard rule:** Trust Layer runs twice per turn — once on input, once on output. Neither check is optional.

**Hard rule:** The async steps (Memory write, Learning emit) happen after the response is delivered.
They must never block or delay the user-facing response.

---

## 5. Module Interaction Rules

| Caller | Calls | For what |
|---|---|---|
| Agent Core | Memory Layer | Read session state at turn start; write state after response |
| Agent Core | Trust Layer | Check input before LLM; check output after LLM |
| Agent Core | Knowledge Engine | Assemble the full prompt before every LLM call (session state passed in request body) |
| Agent Core | Action Gateway | Execute tool calls requested by the LLM |
| Agent Core | Learning Layer | Emit turn metadata asynchronously after response |
| Action Gateway | External systems | Only after receiving intent from Agent Core |

**Note:** Knowledge Engine does **not** call Memory Layer directly. Agent Core fetches session state
from Memory Layer and passes it to Knowledge Engine in the `/assemble_prompt` request body.
Knowledge Engine is stateless — it receives all context it needs in each request.

**No other cross-module calls are defined. Do not introduce new dependencies between blocks.**

---

## 6. Development Guidelines

Follow these rules in every file you write:

1. **Agent Core is the only orchestrator.** No other block initiates calls to other blocks.
   Agent Core fetches session state from Memory Layer and passes it to Knowledge Engine in the
   `/assemble_prompt` request body. Knowledge Engine never calls Memory Layer directly.

2. **Agent Core is the only LLM caller.** No other block calls the LLM directly. All Anthropic API
   interaction goes through `agent_core/src/llm_wrapper/claude_wrapper.py` (`ClaudeLLMWrapper`).
   Agent Core also exposes `POST /internal/llm/call` — a proxy endpoint that other DPG services
   can use in future to get LLM access without holding their own Anthropic API key. Currently
   implemented but not active (not wired into any other service yet).

3. **All external system access goes through Action Gateway.** The LLM expresses intent via tool
   definitions; it never calls APIs directly.

4. **Trust Layer is mandatory on every I/O pass.** Input must be checked before reaching the LLM.
   Output must be checked before reaching the user. Never skip either check.

5. **Agent Core is stateless.** It holds no session state between turns. All state lives in the Memory
   Layer backing store. Agent Core instances can scale horizontally with no coordination.

6. **Learning Layer is always async.** Never call Learning Layer in the response path. Emit events
   after the response is returned to the user.

7. **Domain Configuration Kit drives runtime behaviour.** No model names, persona text, tool
   definitions, guardrail rules, or routing logic are hardcoded. All come from YAML config loaded
   at startup.

8. **Write connectors require consent.** Any Action Gateway connector of type `write` or `identity`
   must be gated by Trust Layer consent rules before execution.

9. **Keep blocks loosely coupled.** Each block has a defined interface. The Agent Core calls blocks
   through that interface only. Do not reach into another block's internals.

10. **Stubs must honour the same interface as real implementations.** Stub method signatures and return
    types must match what the real block would return so they can be replaced without code changes.

---

## 7. Implementation Notes

### Stateless Agent Core
Agent Core instances hold no session state. At the start of every `process_turn()` call, state is loaded
fresh from the Memory Layer. At the end, state is written back. This enables horizontal scaling: any
Agent Core instance can handle any session.

### Config-driven behaviour
The entire runtime is wired from YAML at startup. The Agent Core reads:
- `agent.primary_model` / `agent.fallback_model` — which Claude models to use
- `conversation.persona` — injected into every system prompt
- `connectors.read` / `connectors.write` — becomes the tool definitions sent to the LLM
- `trust.content_safety` / `trust.escalation` — drives Trust Layer decisions
- `knowledge.glossary` / `knowledge.sources` — drives Knowledge Engine retrieval

No domain-specific values appear anywhere in Python source. YAML is the contract.

### Tool Execution Pattern
The LLM signals intent by returning a `tool_use` block. The Agent Core detects this, routes to the
Tool Registry, calls Action Gateway, appends the `tool_result` to messages, and makes a second LLM call.
The LLM never sees raw API responses — only normalised results from Action Gateway.

### Latency Budget
Target: 800–1200ms per turn (voice-first). The Agent Core manages this budget:
- Most turns: one LLM call
- Tool turns: two LLM calls (LLM → tool → LLM)
- Model fallback: switch to faster model if primary is slow or rate-limited

### Routing Model
Routing decisions are primarily made by the LLM through tool selection. The Agent Core provides tool definitions to the LLM, which chooses whether to respond directly, call a tool, or request additional information.

**Note:** Hard routing rules (e.g., escalation topics) are enforced by the Trust Layer *before* the LLM is ever called.

---

## 8. Architecture Boundaries

The following are **intentionally out of scope** for this framework. Do not implement them:

| Out of scope | Reason |
|---|---|
| ASR / TTS pipeline | Framework is text-in, text-out. Voice conversion is upstream/downstream. |
| Model training & fine-tuning | Uses foundation models via API only. |
| Infrastructure provisioning | Framework is infrastructure-agnostic. |
| Multi-tenancy & cost attribution | Platform engineering concern, not framework concern. |
| Testing & simulation tooling | Needed pre-launch but not part of the runtime framework. |
| Versioning & rollback | Config version control is an operational concern. |

---

## 9. PoC Implementation Scope

For the current Proof of Concept, system components are split between full implementations and lightweight stubs.

### Full Modules
- **Knowledge Engine:** RAG retrieval (Glossary, Static KB, Multimodal) and prompt assembly.
- **Agent Core:** Orchestration, Language Normalisation, NLU, and LLM execution.
- **Domain Configuration Kit:** YAML-based runtime wiring.

### Lightweight Stubs
The following blocks are implemented as stubs that mimic real interfaces to allow end-to-end execution:

| Block | Stub Behaviour |
|---|---|
| **Memory Layer** | Simple in-process state store (session data only). |
| **Trust Layer** | Basic rule checks (blocked phrases); no ML engine. |
| **Action Gateway** | Mock API server returning synthetic JSON responses. |
| **Reach Layer** | CLI-based input/output (stdin/stdout). |
| **Learning Layer** | Console logging of events; no full observability pipeline. |

**Critical Rule:** Stub interfaces must exactly match the expected interface of their real implementations. This ensures they can be replaced later without changing the Agent Core or other modules.

---

## 10. Engineering and Coding Standards

These standards apply to every module in the repository. Follow them regardless of which DPG block you are implementing.

---

### Base Class Pattern

Every core component must define a **base class or abstract interface** before any concrete implementation is written.

- The base class defines the required methods, their signatures, and their expected inputs and outputs.
- Concrete implementations must inherit from the base class and implement every required method.
- No concrete implementation may be used by another module unless it inherits from the defined base class.

This ensures all implementations of the same block are interchangeable and structurally predictable.

```python
# Example pattern
from abc import ABC, abstractmethod

class MemoryLayerBase(ABC):

    @abstractmethod
    def read(self, session_id: str) -> dict:
        """Load session state. Returns empty dict if no state exists."""

    @abstractmethod
    def write(self, session_id: str, state: dict) -> None:
        """Persist session state."""
```

---

### Interface Consistency

All classes derived from a base class must:

- Implement every method declared in the base class — no partial implementations.
- Preserve the exact method signature defined in the base class. Do not add, remove, or rename parameters in derived classes.
- Return the same output type and structure that the base class documents. Do not return different shapes from different implementations.

If a method is not applicable in a stub, implement it to return the correct empty/default value — not `NotImplementedError` in production paths.

---

### Module Encapsulation

Each module exposes a **defined public interface only**.

- Other modules interact with a module exclusively through its base class interface or its documented public methods.
- Internal helpers, private utilities, and implementation details must not be imported by other modules.
- Name internal functions with a leading underscore (`_`) to signal they are not part of the public interface.

```python
# Correct: import the public class
from stubs.memory_stub import SessionMemory

# Wrong: reach into internal helpers
from stubs.memory_stub import _build_state_key  # do not do this
```

---

### Base Case Handling

Every function must explicitly handle common edge conditions. Do not assume inputs are well-formed.

Handle the following at minimum:

| Condition | Expected behaviour |
|---|---|
| Empty string input | Return a structured empty result, not an error |
| `None` value for a required parameter | Raise a descriptive `ValueError` immediately |
| Missing key in a dict | Use `.get()` with a safe default; do not access with `[]` blindly |
| Empty list or zero results from a query | Return an empty result with a clear status field |
| Unexpected type from upstream | Log the type mismatch and return a structured error response |

Functions must fail safely. They must never crash the caller with an unhandled exception.

---

### Error Handling

All calls to external systems (LLM API, vector database, ONEST connector, mock servers) must include:

- **Timeout handling** — every external call must have an explicit timeout value. Never use the default (which may be unlimited).
- **Retry logic** — transient failures (rate limits, timeouts) must be retried at least once before escalating. Use exponential backoff where appropriate.
- **Structured error responses** — on failure, return a dict or raise a typed exception that the caller can handle programmatically. Do not return raw exception strings to callers.
- **No silent swallowing** — never use a bare `except: pass`. Always log the error and either re-raise or return a structured failure response.

```python
# Wrong
try:
    result = call_external_api()
except Exception:
    pass

# Correct
try:
    result = call_external_api()
except TimeoutError as e:
    logger.error("API timeout", extra={"error": str(e)})
    raise ExternalCallError("API timed out") from e
```

---

### Configuration Discipline

No domain-specific or environment-specific value may be hardcoded in source code.

- Model names, API endpoints, temperature values, thresholds, blocked phrases, persona text, and timeout values must all come from the YAML config or environment variables.
- Source code may define **defaults** for optional parameters, but any value that varies between deployments must be externally configurable.
- Read all config at startup via `config/loader.py`. Do not re-read config files at runtime inside request paths.

```python
# Wrong
model = "claude-sonnet-4-5-20250514"

# Correct
model = config.agent.primary_model
```

---

### Testing Requirements

Every module must include tests that cover three categories:

| Category | What to test |
|---|---|
| Normal execution | The function returns the correct output for valid, well-formed inputs |
| Edge cases | Empty inputs, boundary values, missing optional fields |
| Failure scenarios | External call failure, invalid config, blocked output, upstream timeout |

- Tests are placed in a `tests/` subdirectory inside the module they validate.
- Tests for external calls must mock the external dependency. Do not make real API calls in unit tests.
- Test file names must match the module they test: `test_llm_wrapper.py` tests `llm_wrapper.py`.
- The test suite must maintain **≥ 70% line coverage** across `agent_core/` and `knowledge_engine/`.

---

### Logging and Observability

Every significant operation must emit a structured log entry. Do not use bare `print()` statements in module code.

Each log entry must include:

| Field | Description |
|---|---|
| `operation` | Name of the function or step being executed |
| `status` | `success`, `failure`, or `skipped` |
| `error` | Error message and type, present only on failure |
| `latency_ms` | Elapsed time in milliseconds, present for external calls and LLM calls |

```python
import logging
import time

logger = logging.getLogger(__name__)

start = time.time()
# ... operation ...
logger.info("llm_call", extra={
    "operation": "llm_wrapper.call",
    "status": "success",
    "latency_ms": int((time.time() - start) * 1000),
    "model": model_used,
    "input_tokens": input_tokens,
    "output_tokens": output_tokens,
})
```

Use `logger.error()` with the `extra` dict for failures. Never log sensitive user data (PII, phone numbers, message content) outside of the designated audit log path managed by the Learning Layer.

