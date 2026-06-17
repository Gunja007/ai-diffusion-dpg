# Changes to Be Made — Scope ONE and TWO

## Overview
This plan outlines the changes required to implement and configure the inbound Model Context Protocol (MCP) channel as part of the Reach Layer for the Decentralised Public Good (DPG) pipeline (GitHub Issue #338). In total, 14 files across the Reach Layer service, the dev-kit schemas, configuration setups, docker orchestration, and documentation must be created or updated to introduce the MCP channel adapter, expose the `dpg.send_message` tool with namespacing and progress tracking, register wizard schemas and rules, and declare containerized services running on port 8007.

---

## Change 1: Add MCP to Runtime Configuration Schema

**File:** [reach_layer/base/schema/config.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/base/schema/config.py)

**Type of change:** Modify

**What to change:** Add `McpServerConfig` and `McpChannelConfig` Pydantic models with `extra="forbid"` to represent the server bindings (host and port) and channel settings. Add the `mcp` channel field to the `ChannelsConfig` container as an optional parameter.

```python
class McpServerConfig(BaseModel):
    """Server bind for the MCP channel."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = "0.0.0.0"
    port: int = Field(default=8007, gt=0, lt=65536)


class McpChannelConfig(BaseModel):
    """MCP channel service config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    assembly_mode: AssemblyMode = AssemblyMode.session
    server: McpServerConfig = Field(default_factory=McpServerConfig)
```

Within `ChannelsConfig`:
```python
mcp: Optional[McpChannelConfig] = None
```

**Why:** The channel settings must be config-driven using the exact fields the block consumes. This addresses Requirement 1.3: "Make the channel config-driven via `reach_layer.channels.mcp` blocks in both the framework schema (`dev-kit/dpg/reach_layer.yaml`) and the runtime schema (`MergedConfig` in `reach_layer/base/schema/config.py`)."

**Audit reference:** "Scope ONE — Requirement 1.3: [Config-driven via `reach_layer.channels.mcp` block in both: `dev-kit/dpg/reach_layer.yaml` (framework schema) and `reach_layer_base.schema.config.MergedConfig` (runtime schema) …since reach config is dual-validated.]"

**Scope:** ONE

---

## Change 2: Expose MCP Domain Section in Dev-Kit Domain Schemas

**File:** [dev-kit/dev_kit/schemas/domain/reach_layer.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/dev-kit/dev_kit/schemas/domain/reach_layer.py)

**Type of change:** Modify

**What to change:** Declare an empty `McpChannelSection` model with `extra="forbid"` and add an optional `mcp` property mapped to it under `ChannelsSection`.

```python
class McpChannelSection(BaseModel):
    """reach_layer.channels.mcp — mcp-channel domain config."""
    model_config = ConfigDict(extra="forbid")
```

Within `ChannelsSection`:
```python
mcp: Optional[McpChannelSection] = None
```

**Why:** Expose the MCP channel section in the domain wizard schema to support deployment wizard validation. This addresses Requirement 1.3: "Make the channel config-driven via `reach_layer.channels.mcp` blocks in both the framework schema (`dev-kit/dpg/reach_layer.yaml`) and the runtime schema."

**Audit reference:** "Scope ONE — Requirement 1.1: [New channel type `mcp` in Reach Layer, alongside `web`, `voice`, `cli`.] ... In `dev-kit/dev_kit/schemas/domain/reach_layer.py` ... `mcp: Optional[McpChannelSection] = None`"

**Scope:** ONE

---

## Change 3: Define MCP Config in Dev-Kit Flat Schema Copy

**File:** [dev-kit/dev_kit/schema.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/dev-kit/dev_kit/schema.py)

**Type of change:** Modify

**What to change:** Add `McpServerConfig` and `McpChannelConfig` classes representing host, port, enabled status, and assembly mode. Remove any out-of-scope fields (such as `auth` and `callers` blocks) to ensure perfect alignment with the runtime config, and add `mcp` to `ChannelsConfig`.

```python
class McpServerConfig(BaseModel):
    """Server bind for the MCP channel."""

    host: str = "0.0.0.0"
    port: int = Field(default=8007, gt=0, lt=65536)


class McpChannelConfig(BaseModel):
    """MCP channel service config."""

    enabled: bool = True
    assembly_mode: str = "session"
    server: McpServerConfig = Field(default_factory=McpServerConfig)
```

Within `ChannelsConfig`:
```python
mcp: McpChannelConfig | None = Field(default=None, description="MCP channel config. None = not deployed.")
```

**Why:** The flat schema copy acts as the host-mode deployment gate and must match the runtime schema properties. This addresses Requirement 1.3: "Make the channel config-driven... in both the framework schema and the runtime schema... since reach config is dual-validated."

**Audit reference:** "Consistency issue 2: Mismatched schema properties between Dev-kit and Runtime ... `dev-kit/dev_kit/schema.py` defines `McpChannelConfig` with `auth` and `callers` fields. `reach_layer/base/schema/config.py` defines `McpChannelConfig` without `auth` and `callers` fields ... resolved by aligning."

**Scope:** ONE

---

## Change 4: Register MCP Field Rules in Dev-Kit Wizard Rules

**File:** [dev-kit/dev_kit/agent/field_rules/reach_layer.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/dev-kit/dev_kit/agent/field_rules/reach_layer.py)

**Type of change:** Modify

**What to change:** Register a deploy-category field rule for `channels.mcp` referencing `McpChannelSection`. Ensure no rules referencing `channels.mcp.callers` or `channels.mcp.auth` exist.

```python
    "channels.mcp": FieldRule(
        category="deploy",
        applies_if='"mcp" in selected_channels',
        description="MCP channel configuration.",
        pydantic_class="McpChannelSection",
    ),
```

**Why:** Ensure that the wizard maps the `mcp` channel selection to the correct Pydantic class without referencing out-of-scope credentials features. This addresses Requirement 1.3: "Make the channel config-driven via `reach_layer.channels.mcp` blocks in both the framework schema... and the runtime schema."

**Audit reference:** "Consistency issue 3: Invalid Field Rules for MCP ... `dev-kit/dev_kit/agent/field_rules/reach_layer.py` contains rules for `"channels.mcp.callers"` and `"channels.mcp.auth"` ... resolved by removing references."

**Scope:** ONE

---

## Change 5: Set MCP Default Settings in Framework Defaults

**File:** [dev-kit/dpg/reach_layer.yaml](file:///Users/samhithrao/projects/ai-diffusion-dpg/dev-kit/dpg/reach_layer.yaml)

**Type of change:** Modify

**What to change:** Add default framework parameters for the `mcp` channel, specifying `enabled: true`, `assembly_mode: session`, `host: 0.0.0.0`, and `port: 8007`.

```yaml
    # ─── MCP ─────────────────────────────────────────────────────────────────
    mcp:
      enabled: true
      assembly_mode: session
      server:
        host: 0.0.0.0
        port: 8007
```

**Why:** Initialize standard default configurations for the MCP adapter during framework setup. This addresses Requirement 1.3: "Make the channel config-driven via `reach_layer.channels.mcp` blocks in both the framework schema (`dev-kit/dpg/reach_layer.yaml`) and the runtime schema."

**Audit reference:** "Scope ONE — Requirement 1.3: [Config-driven via `reach_layer.channels.mcp` block in both: `dev-kit/dpg/reach_layer.yaml` (framework schema) and `reach_layer_base.schema.config.MergedConfig` (runtime schema)...]"

**Scope:** ONE

---

## Change 6: Add MCP Service in Docker Compose Configuration

**File:** [automation/docker/docker-compose.dev.yml](file:///Users/samhithrao/projects/ai-diffusion-dpg/automation/docker/docker-compose.dev.yml)

**Type of change:** Modify

**What to change:** Add the `reach_layer_mcp` service container definition to map port `8007:8007`, bind configuration volumes, link to `dpg_net`, and define service dependencies and health checks.

```yaml
      # ---------------------------------------------------------------------------
      # Reach Layer — MCP channel (port 8007)
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

**Why:** Spin up the MCP channel service as an independent container in development. This addresses Requirement 1.4: "Add a new Docker Compose service entry for the MCP channel service, exposing port `:8007`."

**Audit reference:** "Scope ONE — Requirement 1.4: [New docker-compose service entry, port `:8007` (open to bikeshed).]"

**Scope:** ONE

---

## Change 7: Document MCP Channel in System Architecture

**File:** [ARCHITECTURE.md](file:///Users/samhithrao/projects/ai-diffusion-dpg/ARCHITECTURE.md)

**Type of change:** Modify

**What to change:** Add details of port 8007, the `McpReachLayer` class, files mapping, and status indicators in the tables and overview sections.
- Add `| Reach Layer — MCP | 8007 |` to the Ports table.
- Add `| MCP | direct | MCP tool calls are request-response; session mode unnecessary for v1. |` to the assembly modes table.
- Mark `MCP` as complete in the channels list: `| MCP (reach_layer/mcp/) | ✅ | McpReachLayer — exposes dpg.send_message tool via MCP SSE transport, port 8007. |`
- List key files: `- reach_layer/mcp/src/mcp_reach.py — McpReachLayer`, `- reach_layer/mcp/src/server.py — MCP server with dpg.send_message tool`, and `- reach_layer/mcp/main.py — entrypoint`

**Why:** The high-level developer documentation must stay synchronized with implemented channels. This addresses the overall goal of "exposing the Decentralised Public Good (DPG) pipeline as an inbound channel in the Reach Layer."

**Audit reference:** "File 1: ARCHITECTURE.md ... updates tables, ports list, and flow documentation for the new inbound MCP channel."

**Scope:** Both

---

## Change 8: Register MCP in Developer Runbook

**File:** [CLAUDE.md](file:///Users/samhithrao/projects/ai-diffusion-dpg/CLAUDE.md)

**Type of change:** Modify

**What to change:** Include `Reach Layer MCP :8007` under the Ports block and list MCP as a partially-implemented channel under `PoC scope` / `Channels & Reach`.

**Why:** Keep developer shortcuts and commands up to date with the project's channels. This addresses the overall goal of exposing the MCP channel.

**Audit reference:** "File 2: CLAUDE.md ... maps port `:8007` to MCP and lists MCP as completed under Reach Layer."

**Scope:** Both

---

## Change 9: Create MCP Project Dependencies Configuration

**File:** [reach_layer/mcp/pyproject.toml](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/pyproject.toml)

**Type of change:** Add

**What to change:** Set up the python package metadata and install dependencies for the MCP server: `reach-layer-base`, `httpx`, `pyyaml`, `python-dotenv`, `observability-layer`, `opentelemetry-instrumentation-httpx`, and `mcp>=1.0`. Declare `dev` requirements and coverage tools.

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "reach-layer-mcp"
version = "0.1.0"
description = "Reach Layer — MCP channel adapter."
requires-python = ">=3.11"
dependencies = [
    "reach-layer-base",
    "httpx>=0.27.0",
    "pyyaml>=6.0",
    "python-dotenv>=1.0.0",
    "observability-layer",
    "opentelemetry-instrumentation-httpx>=0.61b0",
    "mcp>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23.0",
    "pytest-cov>=5.0",
    "pytest-mock>=3.0",
    "respx>=0.22.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
asyncio_mode = "auto"

[tool.coverage.run]
source = ["src", "main.py"]
omit = ["*/tests/*", "*/__init__.py"]

[tool.coverage.report]
fail_under = 70
show_missing = true

[tool.uv.sources]
observability-layer = { path = "../../observability_layer" }
reach-layer-base = { path = "../base", editable = true }
```

**Why:** Ensure that the MCP module package can build and resolve relative local path resources properly. This addresses Requirement 1.2: "Implement the MCP channel under the directory `reach_layer/src/mcp/` (or similar) following the established channel abstractions (inheriting from `ReachLayerBase`)."

**Audit reference:** "File 11: reach_layer/mcp/ ... pyproject.toml"

**Scope:** Both

---

## Change 10: Establish MCP Container Configuration

**File:** [reach_layer/mcp/Dockerfile](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/Dockerfile)

**Type of change:** Add

**What to change:** Create a multi-stage Docker build copy of `ghcr.io/astral-sh/uv` to sync environment dependencies, copy the local `observability_layer` and `reach_layer/base` directories, install resources, and expose port 8007 running `main.py`.

```dockerfile
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy
COPY reach_layer/base/ /reach_layer_base/
COPY observability_layer/ /observability_layer/
COPY reach_layer/mcp/pyproject.toml reach_layer/mcp/uv.lock* ./
RUN sed -i 's|path = "../base"|path = "/reach_layer_base"|g; s|path = "../../observability_layer"|path = "/observability_layer"|g' pyproject.toml
RUN uv sync --no-dev --no-cache --no-install-project

FROM python:3.14-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN addgroup --system reachlayer && adduser --system --ingroup reachlayer reachlayer
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /reach_layer_base /reach_layer_base
COPY --from=builder /observability_layer /observability_layer
COPY reach_layer/mcp/main.py ./
COPY reach_layer/mcp/src/ ./src/
COPY reach_layer/config/ ./config/
RUN chown -R reachlayer:reachlayer /app
USER reachlayer
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV=/app/.venv
EXPOSE 8007
CMD ["python", "main.py"]
```

**Why:** Enable isolated deployment of the MCP channel using UV dependencies inside Docker. This addresses Requirement 1.4: "Add a new Docker Compose service entry for the MCP channel service, exposing port `:8007`."

**Audit reference:** "File 11: reach_layer/mcp/ ... Dockerfile"

**Scope:** ONE

---

## Change 11: Create Service Boot Entry Point Script

**File:** [reach_layer/mcp/main.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/main.py)

**Type of change:** Add

**What to change:** Code the entry point logic that reads local defaults (`config/dpg.yaml`) and handles domain environment configs (`domain.yaml` or `CONFIG_FOLDER`), deep-merges them into a channel configuration, and initializes the server block.

```python
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BASE_DIR = _HERE.parent / "base"
if str(_BASE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR.parent))

from reach_layer_base import load_reach_config

_LOCAL_REACH_CONFIG_DIR = _HERE.parent / "config"

def _dpg_config_path() -> Path:
    local = _LOCAL_REACH_CONFIG_DIR / "dpg.yaml"
    if local.exists():
        return local
    return Path("config/dpg.yaml")

def _domain_config_path() -> Path:
    config_folder = os.getenv("CONFIG_FOLDER")
    if config_folder:
        resolved = Path(config_folder) / "reach_layer.yaml"
        if not resolved.exists():
            raise FileNotFoundError(f"CONFIG_FOLDER='{config_folder}' is set but '{resolved}' does not exist.")
        return resolved
    local = _LOCAL_REACH_CONFIG_DIR / "domain.yaml"
    if local.exists():
        return local
    return Path("config/domain.yaml")

def _load_config() -> dict:
    return load_reach_config(
        channel_name="mcp",
        dpg_path=str(_dpg_config_path()),
        domain_path=str(_domain_config_path()),
    )

def main() -> None:
    config = _load_config()
    print("MCP channel starting...")
    from src.server import run_mcp_server
    asyncio.run(run_mcp_server(config))

if __name__ == "__main__":
    main()
```

**Why:** Start the MCP container adapter block and map framework properties at startup. This addresses Requirement 1.2: "Implement the MCP channel under the directory `reach_layer/src/mcp/` (or similar) following the established channel abstractions (inheriting from `ReachLayerBase`)."

**Audit reference:** "File 11: reach_layer/mcp/ ... main.py"

**Scope:** ONE

---

## Change 12: Implement Base Channel Interface Concrete Class

**File:** [reach_layer/mcp/src/mcp_reach.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/src/mcp_reach.py)

**Type of change:** Add

**What to change:** Write the adapter class `McpReachLayer` extending `ReachLayerBase` with empty hooks for starting and ending channel sessions.

```python
from reach_layer.base.reach_layer_base import ReachLayerBase

class McpReachLayer(ReachLayerBase):
    """MCP channel adapter."""

    async def on_session_start(self, session_id: str, user_id: str) -> None:
        """Called when a new session begins. Sets up channel-specific state."""
        pass

    async def on_session_end(self, session_id: str) -> None:
        """Called when a session ends. Tears down channel-specific state."""
        pass
```

**Why:** Follow framework standards by integrating the new channel type under the base channel abstraction. This addresses Requirement 1.2: "Implement the MCP channel under the directory `reach_layer/src/mcp/` (or similar) following the established channel abstractions (inheriting from `ReachLayerBase`)."

**Audit reference:** "Scope ONE — Requirement 1.2: [Lives under `reach_layer/src/mcp/` (or similar) and follows existing channel abstractions.]"

**Scope:** ONE

---

## Change 13: Build the MCP SSE Host Server

**File:** [reach_layer/mcp/src/server.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/src/server.py)

**Type of change:** Add

**What to change:** Implement the Starlette/Uvicorn host running the Model Context Protocol server block.
- Define `dpg.send_message` accepting `session_id`, `message`, `locale`, `caller_agent_id`, and `metadata`.
- Namespace `session_id` using `caller_agent_id` (`mcp_{caller_agent_id}_{session_id}` or `mcp_{session_id}`).
- Map the progress notification token and route streams to `POST /stream_turn` (sentence event parsing) or fallback to `POST /process_turn` (sync).
- Implement timeout and exponential retry handling on HTTP connections.
- Ensure that the original un-namespaced `session_id` is returned.

```python
import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlparse
import httpx
from mcp.server import Server
from mcp.types import Tool, TextContent
from reach_layer.base.reach_layer_base import ReachLayerBase
from reach_layer.base.events import SentenceEvent, DoneEvent

logger = logging.getLogger(__name__)
_config: dict = {}
server = Server("dpg-mcp")

@server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [
        Tool(
            name="dpg.send_message",
            description="Send a message to the DPG pipeline and receive the AI agent's response.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Unique session identifier for the conversation."},
                    "message": {"type": "string", "description": "The user's input message."},
                    "locale": {"type": "string", "description": "Optional locale override."},
                    "caller_agent_id": {"type": "string", "description": "Optional identifier for the calling agent."},
                    "metadata": {"type": "object", "description": "Optional metadata payload."}
                },
                "required": ["session_id", "message"]
            }
        )
    ]

def _get_agent_core_base() -> str:
    ac_config = _config.get("agent_core_client", {})
    endpoint = ac_config.get("endpoint", "http://localhost:8000/process_turn")
    parsed = urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"

def _get_timeout_s() -> float:
    ac_config = _config.get("agent_core_client", {})
    return float(ac_config.get("timeout_s", 30.0))

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name != "dpg.send_message":
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    start = time.time()
    session_id = arguments.get("session_id", "")
    message = arguments.get("message", "")
    caller_agent_id = arguments.get("caller_agent_id", "")
    metadata = arguments.get("metadata", {})

    if not session_id:
        logger.warning("mcp_server.call_tool_missing_session_id", extra={
            "operation": "mcp_server.call_tool", "status": "failure",
            "error": "session_id is required", "latency_ms": int((time.time() - start) * 1000)
        })
        return [TextContent(type="text", text=json.dumps({"error": "session_id is required"}))]

    if not message:
        logger.warning("mcp_server.call_tool_missing_message", extra={
            "operation": "mcp_server.call_tool", "status": "failure",
            "error": "message is required", "latency_ms": int((time.time() - start) * 1000)
        })
        return [TextContent(type="text", text=json.dumps({"error": "message is required"}))]

    if caller_agent_id:
        namespaced_session_id = f"mcp_{caller_agent_id}_{session_id}"
    else:
        namespaced_session_id = f"mcp_{session_id}"

    payload = {
        "session_id": namespaced_session_id,
        "user_message": message,
        "channel": "mcp",
        "user_id": caller_agent_id or "",
        "metadata": metadata,
    }

    agent_core_base = _get_agent_core_base()
    timeout_s = _get_timeout_s()
    ctx = server.request_context.get()
    progress_token = ctx.meta.progressToken if ctx.meta else None

    last_error: Exception | None = None
    final_reply = ""
    event_log = []
    stream_successful = False

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for attempt in range(2):
            try:
                if progress_token:
                    url = f"{agent_core_base}/stream_turn"
                    async with client.stream("POST", url, json=payload) as response:
                        response.raise_for_status()
                        buffer = ""
                        sentences = []
                        async for chunk in response.aiter_text():
                            buffer += chunk
                            while "\n\n" in buffer:
                                event_text, buffer = buffer.split("\n\n", 1)
                                event = ReachLayerBase._parse_sse_event(event_text)
                                if event:
                                    if isinstance(event, SentenceEvent):
                                        sentences.append(event.text)
                                        event_log.append({"type": "sentence", "text": event.text})
                                        await ctx.session.send_progress_notification(
                                            progress_token=progress_token,
                                            progress=event.sentence_index,
                                            message=event.text
                                        )
                                    elif isinstance(event, DoneEvent):
                                        final_reply = "".join(sentences)
                                        stream_successful = True
                                        logger.info("mcp_server.call_tool_success", extra={
                                            "operation": "mcp_server.call_tool", "status": "success",
                                            "session_id": session_id, "latency_ms": int((time.time() - start) * 1000)
                                        })
                                        break
                else:
                    url = f"{agent_core_base}/process_turn"
                    response = await client.post(url, json=payload)
                    response.raise_for_status()
                    result = response.json()
                    final_reply = result.get("response_text", "")
                    stream_successful = True
                    logger.info("mcp_server.call_tool_success", extra={
                        "operation": "mcp_server.call_tool", "status": "success",
                        "session_id": session_id, "latency_ms": int((time.time() - start) * 1000)
                    })

                if stream_successful:
                    break

            except httpx.TimeoutException as e:
                last_error = e
                if attempt == 0:
                    logger.warning("mcp_server.call_tool_timeout_retry", extra={
                        "operation": "mcp_server.call_tool", "status": "failure", "session_id": session_id,
                        "error": f"TimeoutException: {e}", "attempt": attempt + 1,
                        "latency_ms": int((time.time() - start) * 1000)
                    })
                    await asyncio.sleep(1.0)
            except httpx.ConnectError as e:
                last_error = e
                logger.error("mcp_server.call_tool_connect_error", extra={
                    "operation": "mcp_server.call_tool", "status": "failure",
                    "session_id": session_id, "error": f"ConnectError: {e}", "latency_ms": int((time.time() - start) * 1000)
                })
                break
            except Exception as e:
                last_error = e
                logger.error("mcp_server.call_tool_error", extra={
                    "operation": "mcp_server.call_tool", "status": "failure",
                    "session_id": session_id, "error": f"{type(e).__name__}: {e}", "latency_ms": int((time.time() - start) * 1000)
                })
                break

    if not stream_successful:
        error_type = type(last_error).__name__ if last_error else "Unknown"
        error_msg = str(last_error) if last_error else "No response from Agent Core"
        if isinstance(last_error, httpx.TimeoutException):
            user_message = "Agent Core did not respond in time. Please try again."
        elif isinstance(last_error, httpx.ConnectError):
            user_message = "Could not reach Agent Core. Is the backend running?"
        else:
            user_message = f"Unexpected error: {error_type}"

        logger.error("mcp_server.call_tool_final_failure", extra={
            "operation": "mcp_server.call_tool", "status": "failure",
            "session_id": session_id, "error": f"{error_type}: {error_msg}", "latency_ms": int((time.time() - start) * 1000)
        })
        return [TextContent(type="text", text=json.dumps({"error": user_message, "session_id": session_id}))]

    mcp_response = {
        "reply": final_reply,
        "session_id": session_id,
        "finished": False,
        "events": event_log,
    }
    return [TextContent(type="text", text=json.dumps(mcp_response))]

async def run_mcp_server(config: dict) -> None:
    global _config
    _config = config

    from dpg_telemetry import init_otel
    init_otel(service_name="reach_layer.mcp", config=config)

    import uvicorn
    from mcp.server.sse import SseServerTransport

    sse = SseServerTransport("/messages")

    async def app(scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/sse":
                async with sse.connect_sse(scope, receive, send) as streams:
                    await server.run(streams[0], streams[1], server.create_initialization_options())
            elif scope["path"] == "/messages":
                await sse.handle_post_message(scope, receive, send)
            else:
                await send({"type": "http.response.start", "status": 404})
                await send({"type": "http.response.body", "body": b""})

    port = 8007
    try:
        port = config.get("channels", {}).get("mcp", {}).get("server", {}).get("port", 8007)
    except Exception:
        pass

    config_uvicorn = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server_uvicorn = uvicorn.Server(config_uvicorn)
    await server_uvicorn.serve()
```

**Why:** Handles client requests over Server-Sent Events, exposes the `dpg.send_message` tool parameters, translates them into the internal pipeline schema, and manages the SSE connection cleanly. This addresses Requirement 2.1: "Expose a single MCP tool named `dpg.send_message`... and returning the structure...", Requirement 2.2: "Support multi-turn conversations... namespaced by the `caller_agent_id`...", Requirement 2.3: "Map streaming progress notifications...", and Requirement 2.4: "Translate calls within the Reach MCP channel into downstream requests..."

**Audit reference:** "Scope TWO — Requirement 2.1, 2.2, 2.3, 2.4 ... handle_call_tool implementation"

**Scope:** Both

---

## Change 14: Implement the MCP Test Suite

**File:** [reach_layer/mcp/tests/test_mcp_reach.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/tests/test_mcp_reach.py)

**Type of change:** Add

**What to change:** Code unit tests covering tool list querying, parameter validation, namespacing logic, progress notifications, timeout handling, and connection errors. Use `PropertyMock` to configure a mock server context to ensure the progress token is present and the async progress notification channel is tested correctly.

```python
import json
import pytest
import respx
import httpx
from unittest.mock import patch, MagicMock, PropertyMock

from src.server import handle_list_tools, handle_call_tool, _config, server

@pytest.fixture(autouse=True)
def setup_config():
    _config.clear()
    _config.update({
        "agent_core_client": {"endpoint": "http://test-core", "timeout_s": 5.0},
        "reach_layer": {
            "channels": {
                "mcp": {}
            }
        }
    })

@pytest.fixture(autouse=True)
def mock_server_context():
    with patch("mcp.server.lowlevel.server.Server.request_context", new_callable=PropertyMock) as mock_ctx_prop:
        from unittest.mock import AsyncMock
        mock_ctx = MagicMock()
        mock_req_ctx = MagicMock()
        
        mock_meta = MagicMock()
        mock_meta.progressToken = "test-token"
        mock_req_ctx.meta = mock_meta
        
        mock_session = MagicMock()
        mock_session.send_progress_notification = AsyncMock()
        mock_req_ctx.session = mock_session
        
        mock_ctx.get.return_value = mock_req_ctx
        mock_ctx_prop.return_value = mock_ctx
        yield mock_ctx

@pytest.mark.asyncio
async def test_list_tools():
    tools = await handle_list_tools()
    assert len(tools) == 1
    assert tools[0].name == "dpg.send_message"

@pytest.mark.asyncio
@respx.mock
async def test_call_tool_success():
    sse_text = 'event: sentence\ndata: {"type": "sentence", "sentence_index": 0, "text": "Hello world.", "is_final": false}\n\nevent: done\ndata: {"type": "done", "status": "success"}\n\n'
    route = respx.post("http://test-core/stream_turn").mock(
        return_value=httpx.Response(200, text=sse_text)
    )

    args = {
        "session_id": "s1",
        "message": "hi",
        "caller_agent_id": "test-agent"
    }
    
    res = await handle_call_tool("dpg.send_message", args)
    
    assert len(res) == 1
    data = json.loads(res[0].text)
    assert data["reply"] == "Hello world."
    assert data["session_id"] == "s1"
    
    request = route.calls.last.request
    payload = json.loads(request.content)
    assert payload["session_id"] == "mcp_test-agent_s1"
    assert payload["user_id"] == "test-agent"

@pytest.mark.asyncio
async def test_edge_case_missing_session_id():
    args = {"message": "hi"}
    res = await handle_call_tool("dpg.send_message", args)
    data = json.loads(res[0].text)
    assert "session_id is required" in data["error"]

@pytest.mark.asyncio
async def test_edge_case_missing_message():
    args = {"session_id": "s1"}
    res = await handle_call_tool("dpg.send_message", args)
    data = json.loads(res[0].text)
    assert "message is required" in data["error"]

@pytest.mark.asyncio
async def test_edge_case_unknown_tool():
    res = await handle_call_tool("unknown.tool", {})
    data = json.loads(res[0].text)
    assert "Unknown tool" in data["error"]

@pytest.mark.asyncio
@respx.mock
async def test_edge_case_defaults_no_caller_agent_id():
    sse_text = 'event: done\ndata: {"type": "done", "status": "success"}\n\n'
    route = respx.post("http://test-core/stream_turn").mock(
        return_value=httpx.Response(200, text=sse_text)
    )

    args = {"session_id": "s1", "message": "hi"}
    await handle_call_tool("dpg.send_message", args)
    
    request = route.calls.last.request
    payload = json.loads(request.content)
    assert payload["session_id"] == "mcp_s1"
    assert payload["user_id"] == ""

@pytest.mark.asyncio
@respx.mock
async def test_failure_timeout():
    respx.post("http://test-core/stream_turn").mock(
        side_effect=httpx.TimeoutException("Timeout")
    )
    args = {"session_id": "s1", "message": "hi", "caller_agent_id": "test-agent"}
    res = await handle_call_tool("dpg.send_message", args)
    data = json.loads(res[0].text)
    assert "did not respond in time" in data["error"]

@pytest.mark.asyncio
@respx.mock
async def test_failure_connection_refused():
    respx.post("http://test-core/stream_turn").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )
    args = {"session_id": "s1", "message": "hi", "caller_agent_id": "test-agent"}
    res = await handle_call_tool("dpg.send_message", args)
    data = json.loads(res[0].text)
    assert "Could not reach Agent Core" in data["error"]
```

**Why:** Enforce testing requirements by checking correct output, edge validation, and downstream call failures. This addresses the testing requirement of having structured testing for normal and failure execution.

**Audit reference:** "Step 5: Audit every test file ... Consistency issue 1: Test Mocking Mismatch in `test_mcp_reach.py` ... resolved."

**Scope:** Both

---

## Summary of all files to touch

- `[reach_layer/base/schema/config.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/base/schema/config.py)` — Modify to add `McpServerConfig` and `McpChannelConfig` to the runtime schemas.
- `[dev-kit/dev_kit/schemas/domain/reach_layer.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/dev-kit/dev_kit/schemas/domain/reach_layer.py)` — Modify to register empty `McpChannelSection` domain validation segment.
- `[dev-kit/dev_kit/schema.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/dev-kit/dev_kit/schema.py)` — Modify to define host-mode validation structures for `McpChannelConfig` without out-of-scope auth/callers.
- `[dev-kit/dev_kit/agent/field_rules/reach_layer.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/dev-kit/dev_kit/agent/field_rules/reach_layer.py)` — Modify to add `"channels.mcp"` FieldRule and clean up invalid auth/callers rules.
- `[dev-kit/dpg/reach_layer.yaml](file:///Users/samhithrao/projects/ai-diffusion-dpg/dev-kit/dpg/reach_layer.yaml)` — Modify to add standard defaults enabling `reach_layer.channels.mcp`.
- `[automation/docker/docker-compose.dev.yml](file:///Users/samhithrao/projects/ai-diffusion-dpg/automation/docker/docker-compose.dev.yml)` — Modify to add the `reach_layer_mcp` container block exposed on port 8007.
- `[ARCHITECTURE.md](file:///Users/samhithrao/projects/ai-diffusion-dpg/ARCHITECTURE.md)` — Modify to document MCP block interfaces, routing schemes, ports, and channel configuration rules.
- `[CLAUDE.md](file:///Users/samhithrao/projects/ai-diffusion-dpg/CLAUDE.md)` — Modify to map port 8007 to MCP and add the adapter to the PoC implementation checklist.
- `[reach_layer/mcp/pyproject.toml](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/pyproject.toml)` — Add to specify Python package details and dependencies for the MCP service.
- `[reach_layer/mcp/Dockerfile](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/Dockerfile)` — Add to declare builder and runtime container environments for the MCP service.
- `[reach_layer/mcp/main.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/main.py)` — Add to code the configuration-loading bootstrapper entry point.
- `[reach_layer/mcp/src/mcp_reach.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/src/mcp_reach.py)` — Add to define the `McpReachLayer` inheriting from `ReachLayerBase`.
- `[reach_layer/mcp/src/server.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/src/server.py)` — Add to implement the MCP tool endpoints, namespacing logic, and downstream HTTP calls.
- `[reach_layer/mcp/tests/test_mcp_reach.py](file:///Users/samhithrao/projects/ai-diffusion-dpg/reach_layer/mcp/tests/test_mcp_reach.py)` — Add to implement unit tests validating server endpoints and handler logic under normal, edge, and failure scenarios.
