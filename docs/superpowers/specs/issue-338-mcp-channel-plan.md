# Issue #338 — MCP Channel Adapter: Scopes 1, 2 & 3 Implementation Plan

## Background

Issue #338 adds a new **MCP (Model Context Protocol) channel adapter** to the Reach Layer. An MCP host (e.g., Claude Desktop, Cursor) calls into the DPG agent as if it were an MCP server. Scopes 1–3 cover the channel adapter itself, the Agent Core config registration, and all required dev-kit synchronization artifacts.

This plan incorporates the feedback from PR #353's review. The critical failures of the previous plan were:

1. `McpReachLayer` did **not** inherit from `ReachLayerBase` — violating `base-class-pattern.md`.
2. `channels.mcp` was **not** registered in the Agent Core config schema or framework YAML — causing the orchestrator to raise `ValueError: Unsupported channel: mcp` at runtime.
3. Import paths used `reach_layer.base` instead of the installed package name `reach_layer_base`.

Each of those three failures is explicitly addressed in the sections below.

---

## Scope of This Plan

**In scope:** Scopes 1, 2, and 3 only.

| Scope | What it covers |
|---|---|
| **1 — MCP Channel Adapter** | `McpReachLayer` class + packaging (`reach_layer/mcp/`) |
| **2 — Agent Core Channel Registration** | `channels.mcp` in Agent Core runtime schema + framework YAML |
| **3 — Dev-Kit Synchronization** | All dev-kit mirrors, FIELD_RULES, flat schema, domain YAML defaults, Docker Compose service entry |

**Out of scope (this plan):** Trust Layer interaction, MCP protocol depth beyond the tool-call→Agent Core bridge, Helm/Kubernetes resources, front-end tooling.

**GitHub issue #338 sub-scopes deferred to later PRs:**

| #338 sub-scope | Deferred reason |
|---|---|
| Sub-scope 3 — auth & identity (`callers[]`, `caller_agent_id`, API keys) | Requires Trust Layer changes and a separate design review |
| Sub-scope 4 — outbound recipe + gap doc | Depends on auth identity propagation being settled first |
| Sub-scope 5 — observability `peer.*` spans (`peer.agent_id`, `peer.protocol`, `peer.direction`) | Deferred; the logging convention already covers structured fields |
| Sub-scope 6 — public documentation | Written after the implementation is stable |

This plan covers exactly #338 sub-scopes 1 (inbound channel) and 2 (tool surface wiring). Sub-scopes 3–6 are explicitly deferred and must be tracked as follow-up PRs on the same issue.

---

## Open Questions

> **OQ-1 — RESOLVED: MCP assembly mode = `session`.**
> MCP clients send a tool call and expect streaming output back over the same connection. `"session"` mode (POST `/sessions/{id}/input` → SSE events via `subscribe_events()`) is the only fit: it lets the TurnAssembler buffer speech segments and stream `SentenceEvent` payloads back to the MCP caller as they arrive. `"direct"` mode returns a single synchronous JSON blob after the full LLM response — correct for web but wrong for an agent-to-agent streaming protocol. **All references in this plan use `session` mode. This is not configurable per-domain.**

> **OQ-2: MCP server port.**
> The existing channels use `8005` (web) and `8006` (voice). Port `8007` is the natural next assignment. Confirm no other service has claimed `8007`.

> **OQ-3: MCP auth.**
> Does the MCP adapter require API-key-style auth at the HTTP level, similar to `DEVKIT_TO_REACH_API_KEY` in the web channel? Or is auth deferred to a later scope?

> **OQ-4: `user_id` propagation.**
> MCP clients do not natively carry a `user_id`. Should the adapter default to the MCP client's connection identifier, use `None` (anonymous), or accept a config-level default?

> **OQ-5: `selected_channels` enum.**
> Does `"mcp"` need to be added to the `selected_channels` enum in `dev-kit/dev_kit/agent/intake_state.py` so the wizard can present it as a deployment option? If yes, this is a low-risk one-line change in a file listed under "Update if applicable" in `runtime-devkit-sync.md`.

---

## Proposed Changes

### Scope 1 — MCP Channel Adapter (`reach_layer/mcp/`)

The adapter follows the **exact same packaging pattern** as `reach_layer/cli/` and `reach_layer/web/`. Each channel is its own isolated Python project with its own `pyproject.toml`, `Dockerfile`, `main.py`, `src/`, and `tests/`.

---

#### [NEW] `reach_layer/mcp/` (directory tree)

```
reach_layer/mcp/
├── Dockerfile
├── pyproject.toml
├── main.py
├── src/
│   ├── __init__.py
│   └── mcp_reach.py          # McpReachLayer
└── tests/
    ├── __init__.py
    └── test_mcp_reach.py
```

---

#### [NEW] `reach_layer/mcp/src/mcp_reach.py`

**Inheritance chain (mandatory per `base-class-pattern.md`):**

```
ReachLayerBase (ABC)
  └── TextChannelBase
        └── McpReachLayer    ← new
```

**Import path (mandatory, per PR #353 review):**

```python
from reach_layer_base import TextChannelBase
```

`reach_layer_base` is the **installed package name** (verified in `reach_layer/cli/main.py` line 32 and `reach_layer/web/server.py` line 30). Importing from `reach_layer.base` or `reach_layer_base.reach_layer_base` is wrong.

**Mandatory abstract methods** (from `ReachLayerBase`):

| Method | Signature | Notes |
|---|---|---|
| `on_session_start` | `async def on_session_start(self, session_id: str, user_id: str) -> None` | Log MCP connection start |
| `on_session_end` | `async def on_session_end(self, session_id: str) -> None` | Log MCP connection end |
| `run_loop` | `async def run_loop(self) -> None` | Required by `TextChannelBase`; no-op (see below) |

**`__init__` signature:**

```python
def __init__(self, config: dict) -> None:
    super().__init__(config, channel_name="mcp")
    mcp_cfg = (
        config.get("reach_layer", {}).get("channels", {}).get("mcp", {})
        if config else {}
    )
    self._port: int = mcp_cfg.get("port", 8007)
    logger.info(
        "mcp_reach.init",
        extra={
            "operation": "mcp_reach.init",
            "status": "success",
            "channel": "mcp",
            "assembly_mode": self.assembly_mode,
            "port": self._port,
        },
    )
```

`super().__init__` must be called with `channel_name="mcp"`. This satisfies the ABC contract and sets `self._assembly_mode`, `self._agent_core_base`, and `self._timeout_s` from config in the base class.

**`run_loop` design:**

The MCP protocol is server-driven — an external MCP host connects to the adapter's HTTP endpoint. The adapter's `run_loop()` therefore follows the same no-op pattern as `WebReachLayer.run_loop()`: log a `"skipped"` entry and return. The actual event loop is owned by the MCP server library (FastAPI + SSE or the `mcp` SDK).

```python
async def run_loop(self) -> None:
    """No-op. MCP host drives the session lifecycle via the MCP server."""
    logger.info(
        "mcp_reach.run_loop_noop",
        extra={
            "operation": "mcp_reach.run_loop",
            "status": "skipped",
            "reason": "mcp channel is server-driven; server.py owns the loop",
        },
    )
```

**`on_session_start` / `on_session_end`:**

No external resources to acquire at Scope 1. Both methods emit a structured INFO log and return:

```python
async def on_session_start(self, session_id: str, user_id: str) -> None:
    logger.info(
        "mcp_reach.session_start",
        extra={
            "operation": "mcp_reach.on_session_start",
            "status": "success",
            "session_id": session_id,
            "user_id": user_id or "anonymous",
        },
    )

async def on_session_end(self, session_id: str) -> None:
    logger.info(
        "mcp_reach.session_end",
        extra={
            "operation": "mcp_reach.on_session_end",
            "status": "success",
            "session_id": session_id,
        },
    )
```

**Edge-condition handling** (per `base-class-pattern.md`):

| Input condition | Behaviour |
|---|---|
| `config=None` | `ReachLayerBase.__init__` raises `ValueError("config must not be None")` — do not suppress |
| Empty `session_id` | Log a warning and return; do not call Agent Core |
| `user_id=None` | Treat as anonymous; pass `None` to `submit_input` (base class handles it) |
| Missing key in config | Use `.get()` with safe default at every level |

---

#### [NEW] `reach_layer/mcp/src/server.py`

The MCP server module owns the protocol handshake and session lifecycle, analogous to `reach_layer/web/server.py`. It holds a **single `McpReachLayer` instance** created at startup and routes every Agent Core interaction through it.

**Architecture constraints (non-negotiable):**

1. **No module-level channel state.** The server must not store a `_config` dict or a channel instance at module level (e.g., no `server = Server("dpg-mcp")` with a global `_layer` variable). Pass the `McpReachLayer` instance through function arguments or close over it in a factory, exactly as `reach_layer/web/server.py` uses `web_reach` via `create_app(web_reach, config)`.

2. **All Agent Core HTTP calls go through the inherited base-class methods.** Every turn submission must call `mcp_reach.submit_input(session_id, text, user_id)` and every SSE stream must iterate `mcp_reach.subscribe_events(session_id)`. The server must **not** open its own `httpx` client or hand-roll an SSE loop — `ReachLayerBase` already provides a lazy-initialised `AsyncClient` with the correct timeout shape (`read=None` for SSE, connect/write bounded).

3. **`ReachLayerBase._parse_sse_event()` is private — do not call it.** Per `base-class-pattern.md`: "Internal helpers must not be imported by other modules. Prefix internal functions with `_` to signal they are not public." SSE parsing is done automatically inside `subscribe_events()`; the server only ever sees typed `StreamEvent` objects.

**Key responsibilities at Scope 1:**
- Expose `GET /health` returning `{"status": "ok"}` (required for Docker healthcheck).
- Expose a `call_tool` MCP endpoint (or stub) that maps an incoming tool invocation to an Agent Core turn.
- Build `session_id` from the MCP client's connection identifier or generate a UUID per call.
- Call `await mcp_reach.on_session_start(session_id, user_id)` once per logical session.
- Submit the tool input via `await mcp_reach.submit_input(session_id, text, user_id=None)`.
- Consume events via `async for event in mcp_reach.subscribe_events(session_id)` and aggregate.
- Call `await mcp_reach.on_session_end(session_id)` on disconnect or after `DoneEvent.session_ended=True`.

**MCP tool response shape (mandatory — `finished` must be wired):**

The issue's tool contract specifies the response as `{reply, session_id, finished}`. The `finished` field must be wired directly from `DoneEvent.session_ended`:

```python
async def _handle_call_tool(
    mcp_reach: McpReachLayer,
    session_id: str,
    text: str,
) -> dict:
    """Submit a tool call to Agent Core, aggregate the SSE stream."""
    await mcp_reach.submit_input(session_id, text, user_id=None)

    parts: list[str] = []
    finished: bool = False
    async for event in mcp_reach.subscribe_events(session_id):
        if isinstance(event, SentenceEvent):
            parts.append(event.text)
        elif isinstance(event, DoneEvent):
            # Wire session_ended → finished so callers can detect conversation end.
            finished = event.session_ended
            break

    return {
        "reply": " ".join(parts).strip(),
        "session_id": session_id,
        "finished": finished,
    }
```

**Why `finished` must not be hardcoded `False`:** If `finished` is always `False`, MCP callers can never detect that the conversation has ended (e.g., when the agent sends a terminal/hangup subagent response). This breaks caller session lifecycle management and means the MCP client will keep sending turns into an ended session.

> **Note:** The precise MCP JSON-RPC envelope (`params`, `result` structure, tool-discovery `tools/list` response) is a Scope 4+ concern and intentionally omitted here. At Scope 1 the server stub only needs to prove the `McpReachLayer` wiring compiles, `GET /health` responds, and `_handle_call_tool` returns the correct shape.

---

#### [NEW] `reach_layer/mcp/main.py`

Entry point following `reach_layer/cli/main.py` pattern:
- Resolves `dpg.yaml` / `domain.yaml` paths (Docker vs. local checkout).
- Calls `load_reach_config("mcp", dpg_path=..., domain_path=...)`.
- Instantiates `McpReachLayer(config)`.
- Starts the MCP server (uvicorn or equivalent) on `config["reach_layer"]["channels"]["mcp"]["port"]`.

---

#### [NEW] `reach_layer/mcp/pyproject.toml`

Mirrors `reach_layer/cli/pyproject.toml`. Dependencies:
- `reach_layer_base` (path dep pointing at `../base`)
- `fastapi`, `uvicorn[standard]`, `httpx`, `python-dotenv`

---

#### [NEW] `reach_layer/mcp/Dockerfile`

Mirrors `reach_layer/cli/Dockerfile` and `reach_layer/web/Dockerfile`:
- Base image: `python:3.12-slim`
- Copy `reach_layer/base/` → install `reach_layer_base` package
- Copy `reach_layer/mcp/` → install
- `CMD ["python", "main.py"]`

---

#### [NEW] `reach_layer/mcp/tests/test_mcp_reach.py`

Per `testing-requirements.md`, required test cases:

**`McpReachLayer` unit tests:**

| Test | What it verifies |
|---|---|
| `test_init_reads_port_from_config` | `McpReachLayer(config)` reads port from `reach_layer.channels.mcp.port` |
| `test_init_defaults_port` | Missing `mcp` config key → port defaults to 8007 |
| `test_init_raises_on_none_config` | `config=None` → `ValueError` (from base) |
| `test_init_calls_super_with_mcp_channel_name` | `self.channel_name == "mcp"` |
| `test_init_assembly_mode_is_session` | `self.assembly_mode == "session"` (not `direct`) |
| `test_on_session_start_logs` | `on_session_start` does not raise and emits a structured log |
| `test_on_session_end_logs` | `on_session_end` does not raise and emits a structured log |
| `test_run_loop_is_noop` | `run_loop()` returns without raising |
| `test_abc_contract_enforced` | Instantiating an incomplete subclass (missing abstract methods) raises `TypeError` |

**`server._handle_call_tool` unit tests** (mock `submit_input` + `subscribe_events`):

| Test | What it verifies |
|---|---|
| `test_handle_call_tool_aggregates_sentences` | Multiple `SentenceEvent` texts are joined and returned in `reply` |
| `test_handle_call_tool_finished_false_when_session_not_ended` | `finished=False` when `DoneEvent.session_ended=False` |
| `test_handle_call_tool_finished_true_when_session_ended` | `finished=True` when `DoneEvent.session_ended=True` |
| `test_handle_call_tool_empty_reply_on_no_sentences` | `DoneEvent` with no preceding `SentenceEvent` → `reply=""`, not an error |
| `test_handle_call_tool_does_not_call_parse_sse_event_directly` | `ReachLayerBase._parse_sse_event` is never called by `_handle_call_tool` |

---

### Scope 2 — Agent Core Channel Registration

The Agent Core orchestrator calls `_resolve_channel_config(turn_input.channel)` on every turn (`orchestrator.py` L2440–L2462). If the channel name is not in `self._config["channels"]`, it raises `ValueError: Unsupported channel: mcp`. The fix requires registering `"mcp"` in two places.

---

#### [MODIFY] `agent_core/src/schema/config.py`

**Target: `ChannelsConfig` class (lines 599–606)**

Add `mcp: ChannelConfig` with a default factory alongside `voice`, `web`, `cli`:

```python
class ChannelsConfig(BaseModel):
    """Per-channel config block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    voice: ChannelConfig = Field(default_factory=ChannelConfig)
    web: ChannelConfig = Field(default_factory=ChannelConfig)
    cli: ChannelConfig = Field(default_factory=ChannelConfig)
    mcp: ChannelConfig = Field(default_factory=ChannelConfig)  # GH-338
```

`ChannelConfig` (lines 576–597) holds `system_prompt_suffix`, optional `tts_rules`, `max_tokens`, and `turn_assembler`. All fields have sensible defaults so `mcp: {}` in a domain YAML is valid without specifying anything further.

> **Critical:** This is a runtime schema change. Per `runtime-devkit-sync.md`, it **must** be mirrored in the same PR across all dev-kit touch-points (see Scope 3).

---

#### [MODIFY] `dev-kit/dpg/agent_core.yaml`

Add the `mcp` channel block under the top-level `channels:` section. The framework default provides the minimum required keys so the orchestrator's `.get("mcp")` returns a non-`None` dict at runtime:

```yaml
channels:
  voice:                     # existing block unchanged
    ...
  web:                       # existing block unchanged
    ...
  cli:                       # existing block unchanged
    ...
  mcp:                       # GH-338
    system_prompt_suffix: ""
    turn_assembler:
      silence_trigger:
        silence_ms: 400      # session mode: 400ms of silence triggers a turn
      max_wait_ceiling:
        max_wait_ms: 8000    # session mode: absolute max wait
```

> **Note:** `silence_ms: 400` and `max_wait_ms: 8000` are set for `session` mode (same defaults as `cli`). Assembly mode is resolved as `session` — see OQ-1 resolution above.

---

### Scope 3 — Dev-Kit Synchronization

Per `runtime-devkit-sync.md`, every runtime schema change must be reflected in **all four mandatory touch-points** in the same PR.

---

#### 3a. Reach Layer Runtime Schema

#### [MODIFY] `reach_layer/base/schema/config.py`

**Add `McpChannelConfig`** (after `VoiceChannelConfig`, before the `ChannelsConfig` class):

```python
# ---------------------------------------------------------------------------
# MCP channel (GH-338)
# ---------------------------------------------------------------------------

class McpChannelConfig(BaseModel):
    """MCP channel service config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    assembly_mode: AssemblyMode = AssemblyMode.session
    port: int = Field(default=8007, gt=0, lt=65536)
```

**Modify `ChannelsConfig`** (lines 360–374):

```python
class ChannelsConfig(BaseModel):
    """All channel service configs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cli: Optional[CliChannelConfig] = None
    web: Optional[WebChannelConfig] = None
    voice: Optional[VoiceChannelConfig] = None
    mcp: Optional[McpChannelConfig] = None      # GH-338
```

`Optional` is correct — all channels use `Optional` so deployments that omit `mcp` from their config pass schema validation. The runtime `load_reach_config("mcp")` defaults an empty dict via `channels.setdefault("mcp", {})`.

> **Constraint:** `McpChannelConfig` may import **only** from `pydantic`, `enum`, `typing`, `__future__`. No relative imports, no third-party deps, no reach into siblings. (`runtime-devkit-sync.md` §"Runtime schemas must stay self-contained".)

---

#### 3b. Dev-Kit Per-Block Domain Mirror (Reach Layer)

#### [MODIFY] `dev-kit/dev_kit/schemas/domain/reach_layer.py`

**Add `McpChannelSection`** (after `CliChannelSection`):

```python
class McpChannelSection(BaseModel):
    """reach_layer.channels.mcp — MCP channel domain config (GH-338).

    No user-configurable fields at Scope 1. The class must exist so
    update_config(path="reach_layer.channels.mcp", value={}) does not
    fail the per-write pydantic gate with 'extra_forbidden'.
    """
    model_config = ConfigDict(extra="forbid")
```

**Modify `ChannelsSection`**:

```python
class ChannelsSection(BaseModel):
    """reach_layer.channels — at most one entry per channel type."""
    model_config = ConfigDict(extra="forbid")
    web: Optional[WebChannelSection] = None
    voice: Optional[VoiceChannelSection] = None
    cli: Optional[CliChannelSection] = None
    mcp: Optional[McpChannelSection] = None    # GH-338
```

---

#### 3c. FIELD_RULES (Reach Layer)

#### [MODIFY] `dev-kit/dev_kit/agent/field_rules/reach_layer.py`

Add a `deploy`-category port entry for MCP (no chat-phase fields at Scope 1):

```python
# ── Deploy: mcp ───────────────────────────────────────────────────────────

"channels.mcp.port": FieldRule(
    category="deploy",
    applies_if='"mcp" in selected_channels',
    description="Port the MCP channel service binds to. Default: 8007.",
    pydantic_class="McpChannelSection",
),
```

The `assembly_mode` field is framework-default-only (always `session`). It is never written by the wizard or operator — the runtime default is authoritative and no `FIELD_RULE` entry is needed.

---

#### 3d. Flat-File Dev-Kit Schema Copy

#### [MODIFY] `dev-kit/dev_kit/schema.py`

**Two changes in this file:**

**Change 1 — `ChannelsConfig` (Reach Layer, line ~1248):**

Add `mcp: McpChannelConfig | None` field and add the `McpChannelConfig` class before `ChannelsConfig`:

```python
class McpChannelConfig(BaseModel):
    """Configuration for the MCP channel adapter (GH-338).

    Mirrors runtime ``reach_layer/base/schema/config.py:McpChannelConfig``.
    """
    port: int = Field(default=8007, description="Port the MCP server binds to")


class ChannelsConfig(BaseModel):
    """Per-channel configuration. Omit channels that are not deployed."""

    cli: CLIChannelConfig | None = Field(default=None, ...)
    web: WebChannelConfig | None = Field(default=None, ...)
    voice: VoiceChannelConfig | None = Field(default=None, ...)
    mcp: McpChannelConfig | None = Field(default=None, description="MCP channel config. None = not deployed.")  # GH-338
```

**Change 2 — `ChannelsTopLevelConfig` (Agent Core, line ~525):**

Add `mcp: ChannelConfig` to the Agent Core channels flat-file class:

```python
class ChannelsTopLevelConfig(BaseModel):
    """Top-level per-channel configuration block (GH-137).

    Runtime agent_core/src/schema/config.py:ChannelsConfig carries
    voice, web, cli, and mcp (GH-338).
    """

    voice: ChannelConfig = Field(...)
    web: ChannelConfig = Field(...)
    cli: ChannelConfig = Field(...)
    mcp: ChannelConfig = Field(default_factory=ChannelConfig)    # GH-338
```

> **Warning:** `dev-kit/dev_kit/schema.py` is the host-mode deploy gate. Drift here means host-mode deployments silently pass configs the runtime would reject at boot. This file must be updated in the same commit as the runtime schemas.

---

#### 3e. Framework YAML (Reach Layer)

#### [MODIFY] `dev-kit/dpg/reach_layer.yaml`

Add the MCP channel default block (after the `voice:` block):

```yaml
# ─── MCP (GH-338) ──────────────────────────────────────────────────────────
mcp:
  enabled: true
  assembly_mode: session   # MCP host drives sessions; TurnAssembler handles buffering
  port: 8007
```

This provides the framework-level defaults that `load_reach_config("mcp")` merges before domain overrides are applied.

---

#### 3f. Docker Compose Service Entry

#### [MODIFY] `automation/docker/docker-compose.dev.yml`

Add the `reach_layer_mcp` service block (after `reach_layer_voice`):

```yaml
# ---------------------------------------------------------------------------
# Reach Layer — MCP channel (port 8007) — GH-338
# MCP server: receives Model Context Protocol tool calls from MCP hosts
# (Claude Desktop, Cursor, etc.) and routes them to Agent Core.
# ---------------------------------------------------------------------------
reach_layer_mcp:
  image: sanketikahub/dpg-reach-layer-mcp:latest
  pull_policy: missing
  container_name: reach_layer_mcp
  environment:
    - CONFIG_FOLDER=/app/config
  ports:
    - "8007:8007"
  volumes:
    - ../../dev-kit/dpg/reach_layer.yaml:/app/config/dpg.yaml:ro
    - ../../dev-kit/configs/${DOMAIN:-kkb}/reach_layer.yaml:/app/config/domain.yaml:ro
  networks:
    - dpg_net
  deploy:
    resources:
      limits:
        cpus: '0.25'
        memory: 256M
  depends_on:
    agent_core:
      condition: service_healthy
  healthcheck:
    test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8007/health', timeout=5)"]
    interval: 15s
    timeout: 15s
    retries: 5
    start_period: 90s
  restart: unless-stopped
```

The same entry should be added to `automation/docker/docker-compose.yml` (the production compose file).

---

#### 3g. Agent Core Domain Mirror

#### [MODIFY] `dev-kit/dev_kit/schemas/domain/agent_core.py`

Add `mcp` to the `ChannelsSection` class (mirrors `agent_core/src/schema/config.py:ChannelsConfig`):

```python
mcp: Optional[ChannelConfig] = None    # GH-338
```

The existing `ChannelConfig` class in this file already covers `system_prompt_suffix`, `tts_rules`, and `turn_assembler`. No new class is needed.

---

#### 3h. FIELD_RULES (Agent Core)

#### [MODIFY] `dev-kit/dev_kit/agent/field_rules/agent_core.py`

Add `mcp` channel rules, gated on `"mcp" in selected_channels`:

```python
# ── Gated chat: channels.mcp.* ──────────────────────────────────────────────

"channels.mcp.system_prompt_suffix": FieldRule(
    category="chat",
    phase="language",
    applies_if='"mcp" in selected_channels',
    description="System prompt suffix for MCP channel.",
    invalidated_by=["default_language", "supported_languages"],
    pydantic_class="ChannelsSection",
),

# ── Predetermined: channels.mcp.turn_assembler.* ────────────────────────────

"channels.mcp.turn_assembler.silence_trigger.silence_ms": FieldRule(
    category="predetermined",
    rule='set: 400 if "mcp" in selected_channels else None',
    invalidated_by=["selected_channels"],
    pydantic_class="ChannelsSection",
),
"channels.mcp.turn_assembler.max_wait_ceiling.max_wait_ms": FieldRule(
    category="predetermined",
    rule='set: 8000 if "mcp" in selected_channels else None',
    invalidated_by=["selected_channels"],
    pydantic_class="ChannelsSection",
),
```

> **Note:** `silence_ms: 400` / `max_wait_ms: 8000` are the correct values for `session` mode. Assembly mode is definitively `session` — see OQ-1 resolution.

---

## Execution Order

The following order is mandatory because later steps depend on earlier schema definitions.

| Step | File | Why this order |
|---|---|---|
| 1 | `reach_layer/base/schema/config.py` | Runtime schema is the source of truth; must exist before mirrors |
| 2 | `reach_layer/mcp/` (full package) | Depends on `reach_layer_base` package from step 1's directory |
| 3 | `agent_core/src/schema/config.py` | Adds `channels.mcp` to the orchestrator's validation schema |
| 4 | `dev-kit/dpg/agent_core.yaml` | Framework default for the new schema key |
| 5 | `dev-kit/dpg/reach_layer.yaml` | Framework default for the new reach layer schema key |
| 6 | `dev-kit/dev_kit/schemas/domain/reach_layer.py` | Domain mirror — must exist before FIELD_RULES reference it |
| 7 | `dev-kit/dev_kit/schemas/domain/agent_core.py` | Domain mirror — must exist before FIELD_RULES reference it |
| 8 | `dev-kit/dev_kit/agent/field_rules/reach_layer.py` | FIELD_RULES reference pydantic_class names from step 6 |
| 9 | `dev-kit/dev_kit/agent/field_rules/agent_core.py` | FIELD_RULES reference pydantic_class names from step 7 |
| 10 | `dev-kit/dev_kit/schema.py` | Flat-file copy of both runtime schemas — updated last to catch final drift |
| 11 | `automation/docker/docker-compose.dev.yml` | Service entry references the image built from step 2 |
| 12 | `automation/docker/docker-compose.yml` | Production compose — same entry |

---

## Assumptions and Risks

| Item | Assumption / Risk | Mitigation |
|---|---|---|
| Port 8007 | Assumed available. | Verify against all compose files before cutting code. |
| MCP assembly mode | **Resolved: `session`.** No further confirmation needed. | — |
| `finished` wiring | If `_handle_call_tool` does not wire `DoneEvent.session_ended → finished`, MCP callers cannot detect conversation end and will keep sending turns into a closed session. | Covered by test `test_handle_call_tool_finished_true_when_session_ended`. |
| `_parse_sse_event` private method | If `server.py` calls `ReachLayerBase._parse_sse_event(...)` directly, it violates `base-class-pattern.md` and couples to an internal. | Covered by test `test_handle_call_tool_does_not_call_parse_sse_event_directly`. |
| Module-level channel globals | If `server.py` uses `global _layer` or `global _config`, the test suite cannot inject mock channels and integration tests are impossible. | Architecture constraint is explicit in the server.py section; enforced by the server test fixtures using `create_app`-style injection. |
| `user_id` | Plan passes `None` for anonymous MCP clients. | If the MCP transport carries a client identity (deferred to #338 sub-scope 3), thread it through `on_session_start` and `submit_input`. |
| `McpChannelSection` is empty | Domain mirror has no chat fields at Scope 1 — wizard skips it. | Acceptable; CLI also has minimal chat-phase fields at Scope 1. |
| Trust Layer | Not modelled. MCP turns pass through the same Trust Layer path as other channels. | No change needed to Trust Layer for Scopes 1–3. |
| `selected_channels` enum | FIELD_RULES use `"mcp" in selected_channels`. If `intake_state.py` does not include `"mcp"`, the `applies_if` conditions are never true and wizard silently skips MCP FIELD_RULES. | Add `"mcp"` to `intake_state.py` (OQ-5, low-risk one-line change). |
| Docker image name | Plan uses `sanketikahub/dpg-reach-layer-mcp:latest` as a placeholder. | Update to the actual image name once CI is configured. |
| #338 sub-scopes 3–6 deferred | Auth/identity, outbound recipe, observability spans, and docs are explicitly out of scope here. | Tracked as follow-up work on the same GitHub issue. |

---

## Verification Plan

### Automated Tests

```bash
# Unit tests for the new adapter
cd reach_layer/mcp && python -m pytest tests/ -v

# Runtime schema validation — MergedConfig accepts mcp channel
cd reach_layer && python -c "
from base.schema.config import MergedConfig
MergedConfig.validate_full({
    'reach_layer': {
        'channels': {'mcp': {'port': 8007}},
        'common': {},
    }
})
print('reach_layer schema: OK')
"

# Agent Core schema validation — mcp channel registers
cd agent_core && python -c "
from src.schema.config import MergedConfig
cfg = MergedConfig.validate_full({'channels': {'mcp': {}}})
assert hasattr(cfg.channels, 'mcp'), 'mcp missing from channels'
print('agent_core schema: OK')
"

# Dev-kit host-mode schema validation
cd dev-kit && python -c "
from dev_kit.schema import ChannelsConfig as ReachChannels
from dev_kit.schema import ChannelsTopLevelConfig as AgentChannels
ReachChannels(mcp={'port': 8007})
AgentChannels(mcp={})
print('dev-kit schema: OK')
"

# Existing tests must still pass
cd agent_core && python -m pytest tests/ -v
cd reach_layer && python -m pytest tests/ -v
cd dev-kit && python -m pytest tests/ -v
```

### Manual Verification

1. **Rebuild the dev-kit image** after the runtime schema change:
   ```bash
   docker build -f dev-kit/Dockerfile -t dpg-dev-kit .
   ```

2. **Start all services** including `reach_layer_mcp`:
   ```bash
   docker compose -f automation/docker/docker-compose.dev.yml up -d
   ```

3. **Confirm `reach_layer_mcp` healthcheck** passes:
   ```bash
   docker inspect --format='{{.State.Health.Status}}' reach_layer_mcp
   # expected: healthy
   ```

4. **Confirm Agent Core accepts a turn** with `channel="mcp"` (previously this raised `Unsupported channel: mcp`):
   ```bash
   curl -s -X POST http://localhost:8000/process_turn \
     -H "Content-Type: application/json" \
     -d '{"session_id":"test-mcp","user_message":"hello","channel":"mcp"}' | jq .
   # expected: response_text is non-empty, error_type != "unsupported_channel"
   ```

5. **Run the dev-kit wizard** end-to-end with `mcp` in `selected_channels`. Deploy response must show `"validator": "runtime_baked"`.
