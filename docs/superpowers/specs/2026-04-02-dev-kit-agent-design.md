# Dev Kit Conversation Agent — Design Spec

**Issue:** #11
**Date:** 2026-04-02

## Problem

A non-technical user who wants to deploy a conversational AI on the DPG framework currently has to understand 7 YAML config files, the Pydantic schemas, and the KKB reference implementation — then manually author their own domain configs. This is a high barrier for adoption.

## Goal

Build an LLM-powered conversation agent inside `dev-kit/` that interviews the user about their use case and generates a complete set of domain configs under `dev-kit/configs/<project>/`. The agent is a standalone deployable service with a web UI.

## Key Decisions

- **LLM-powered (Claude API)** using tool_use for structured config extraction alongside natural conversation.
- **Modular conversation**: infrastructure configs first (language, knowledge, memory, trust, connectors), then a separate workflow designer phase for the subagent graph.
- **Best-effort generation with draft flags** for the 4 blocks whose templates are not yet finalized (action_gateway, reach_layer, trust_layer, learning_layer). Generated with `# STATUS: draft — block template not yet finalized` header.
- **File-system persistence** — no database. Each project is a folder under `dev-kit/configs/<project>/` with a `_meta/` directory for conversation state and checkpoints.
- **FastAPI backend + React SPA frontend** served as a single deployable container.

---

## Architecture

### Directory Structure

```
dev-kit/
├── agent/                    # Conversation agent backend
│   ├── __init__.py
│   ├── app.py                # FastAPI app — serves API + static frontend
│   ├── conversation.py       # Claude conversation engine (tool-use loop)
│   ├── tools.py              # Tool definitions + handlers
│   ├── prompts/              # System prompt templates per phase
│   │   ├── base.py           # Shared context (DPG overview, schema summaries)
│   │   ├── overview.py       # Phase 1: domain understanding
│   │   ├── infrastructure.py # Phases 2-6: language, knowledge, memory, trust, connectors
│   │   └── workflow.py       # Phase 7: subagent/routing design
│   ├── accumulator.py        # Config accumulator — dict that builds toward valid YAML
│   ├── checkpoints.py        # Snapshot/restore of accumulator + conversation summary
│   ├── renderer.py           # Accumulator → YAML file writer (uses schema.py for validation)
│   └── static/               # Frontend SPA build output (served by FastAPI)
├── frontend/                 # React SPA source
│   ├── package.json
│   ├── src/
│   │   ├── App.jsx
│   │   ├── Chat.jsx          # Chat interface component
│   │   ├── Dashboard.jsx     # Project config overview with status badges
│   │   ├── FlowGraph.jsx     # Subagent state machine visualization
│   │   └── ConfigEditor.jsx  # YAML viewer/editor per config file
│   └── ...
├── configs/                  # Generated project configs
│   ├── kkb/                  # Reference domain (existing)
│   └── <new_project>/
│       ├── _meta/
│       │   ├── project.json
│       │   └── checkpoints/
│       ├── agent_core.yaml
│       ├── knowledge_engine.yaml
│       ├── memory_layer.yaml
│       ├── trust_layer.yaml        # STATUS: draft
│       ├── action_gateway.yaml     # STATUS: draft
│       ├── reach_layer.yaml        # STATUS: draft
│       └── learning_layer.yaml     # STATUS: draft
├── dpg/                      # Framework defaults (existing)
├── loader.py                 # Existing — minor updates
├── schema.py                 # Existing — minor updates
└── pyproject.toml
```

### Data Flow

```
User (browser) → FastAPI API → Conversation Engine → Claude API (tool_use)
                                    ↓
                              Tool handlers
                                    ↓
                              Accumulator (in-memory dict)
                                    ↓
                              Renderer → YAML files + _meta/
                                    ↓
                              Dashboard API ← User (browser)
```

---

## Conversation Engine

### Conversation Loop

1. User sends message via `POST /api/projects/{slug}/chat`.
2. Backend assembles the messages array:
   - System prompt: base context + phase-specific schema additions + current accumulator state summary.
   - Conversation history: checkpoint summaries for prior phases + last 10 messages for current phase.
   - User message.
3. Claude API call with tool definitions.
4. Process response:
   - Extract text content → send to frontend as agent message.
   - Extract `tool_use` blocks → execute each tool handler against the accumulator.
   - Tool handlers update accumulator → trigger YAML re-render via renderer.
   - If tool results need a follow-up LLM call, loop back to step 3.
5. Return response to frontend: `{reply, phase, config_updates[], checkpoint_created, graph}`.

### Tool Definitions

| Tool | Purpose | When Used |
|---|---|---|
| `set_project_meta` | Set project name, description, domain slug | Phase 1 (overview) |
| `update_config` | Write a section of any block's config | Phases 2-6 (infrastructure) |
| `set_phase` | Advance to next phase, triggers checkpoint | Phase transitions |
| `create_subagent` | Define a new subagent node in the workflow | Phase 7 (workflow) |
| `update_subagent` | Modify an existing subagent's fields | Phase 7 |
| `add_routing_rule` | Add a transition edge between subagents | Phase 7 |
| `update_routing_rule` | Modify an existing routing rule | Phase 7 |
| `remove_subagent` | Delete a subagent and its routing rules | Phase 7 |
| `finalize_config` | Mark a config file as complete (not draft) | Any phase |
| `rollback_to_checkpoint` | Restore accumulator to a previous checkpoint | User-initiated |

### Tool Schemas

**`update_config`:**
```json
{
  "name": "update_config",
  "description": "Update a section of a block's domain config. Values are deep-merged into the current accumulator state for that block.",
  "input_schema": {
    "type": "object",
    "properties": {
      "block": {
        "type": "string",
        "enum": ["agent_core", "knowledge_engine", "memory_layer", "trust_layer", "action_gateway", "reach_layer", "learning_layer"]
      },
      "section": {
        "type": "string",
        "description": "Dot-notation path to the config section, e.g. 'preprocessing.nlu_processor' or 'conversation'"
      },
      "values": {
        "type": "object",
        "description": "Key-value pairs to merge into the section"
      }
    },
    "required": ["block", "section", "values"]
  }
}
```

**`create_subagent`:**
```json
{
  "name": "create_subagent",
  "description": "Add a new subagent to the agent_workflow. The subagent appears as a node in the conversation flow graph.",
  "input_schema": {
    "type": "object",
    "properties": {
      "id": { "type": "string" },
      "name": { "type": "string" },
      "description": { "type": "string" },
      "is_start": { "type": "boolean", "default": false },
      "is_terminal": { "type": "boolean", "default": false },
      "valid_intents": { "type": "array", "items": { "type": "string" } },
      "tools": { "type": "array", "items": { "type": "string" } },
      "system_prompt": { "type": "string" }
    },
    "required": ["id", "name", "description", "system_prompt"]
  }
}
```

**`add_routing_rule`:**
```json
{
  "name": "add_routing_rule",
  "description": "Add a routing rule (transition edge) from a subagent to another subagent, triggered by an intent.",
  "input_schema": {
    "type": "object",
    "properties": {
      "from_subagent_id": { "type": "string" },
      "intent": { "type": "string" },
      "next_subagent_id": { "type": "string" },
      "conditions": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "field": { "type": "string" },
            "operator": { "type": "string", "enum": ["eq", "not_eq", "gt", "lt", "gte", "lte"] },
            "value": {}
          }
        },
        "description": "Optional conditions on session state that must be true for this rule to match"
      },
      "session_writes": {
        "type": "object",
        "description": "Optional session field writes triggered when this rule matches"
      }
    },
    "required": ["from_subagent_id", "intent", "next_subagent_id"]
  }
}
```

### Context Management

When the conversation crosses a checkpoint boundary:
1. Full conversation history is saved to `_meta/checkpoints/<phase_number>_<phase_name>/history.json`.
2. A summary of decisions made is generated deterministically from the accumulator — not an additional LLM call.
3. Active conversation history is truncated to: checkpoint summary + last 10 messages.
4. The accumulator state is always included in the system prompt, so no config decisions are lost.

Estimated token budget per API call:

| Component | Tokens |
|---|---|
| Base prompt | ~500 |
| Phase-specific schema context | ~300–800 |
| Accumulator state summary | ~200–1000 |
| Checkpoint summaries (prior phases) | ~100 per phase |
| Conversation history (last 10 messages) | ~2000 |
| Tool definitions | ~1500 |
| **Total** | **~5000–7000** |

---

## Conversation Phases

### Phase 1: Domain Overview
- Agent asks open-ended questions about the use case, users, domain, and goals.
- Builds a mental model and confirms understanding before proceeding.
- **Output:** `project.json` with name, description, user persona, domain summary.

### Phase 2: Language & Models
- Primary/fallback model selection, supported languages, transliteration needs.
- **Writes:** `agent_core.agent`, `agent_core.preprocessing.language_normalisation`.

### Phase 3: Knowledge & Data Sources
- What knowledge does the AI need? Documents, databases, live APIs?
- Glossary mappings (colloquial terms → canonical identifiers).
- **Writes:** `knowledge_engine.yaml` — glossary, static_knowledge_base, sources, intent_filters.

### Phase 4: User Profile & Memory
- What to remember about users? Session fields, persistent profile fields.
- Graph structure: subnodes, relationships, declared fields.
- **Writes:** `memory_layer.yaml` — session schema, persistent graph, merge_on_session_end.

### Phase 5: Safety & Trust Rules
- Blocked phrases, escalation topics, output restrictions.
- **Writes:** `trust_layer.yaml` (STATUS: draft).

### Phase 6: External Connectors
- External APIs/services the AI calls. Read/write/identity connector definitions.
- **Writes:** `agent_core.connectors`, `action_gateway.yaml` (STATUS: draft).

### Phase 7: Workflow Design
The most complex phase. Builds the subagent graph step by step:
1. Agent proposes an initial flow based on everything learned in prior phases.
2. Flow graph renders visually — user sees nodes and edges immediately.
3. Walk through each subagent: purpose, system prompt, valid intents, routing rules.
4. User can add, remove, or reorder subagents and adjust routing.
5. Agent suggests intents and entities based on the workflow design.
- **Writes:** `agent_core.agent_workflow`, `agent_core.preprocessing.nlu_processor` (intents, entities, sentiment_classes).
- **Also generates:** `reach_layer.yaml`, `learning_layer.yaml` (both STATUS: draft, minimal).

### Phase 8: Review & Validate
- Agent runs schema validation via `loader.py` / `schema.py`.
- Dashboard shows all 7 configs with status badges.
- User can edit any config directly or re-enter conversation at any checkpoint.

### Checkpoint Mechanics

Each checkpoint saves to `_meta/checkpoints/<phase_number>_<phase_name>/`:
```
_meta/checkpoints/
  01_overview/
    accumulator.json    # Full config accumulator state
    summary.txt         # Natural language summary of decisions
    timestamp.json      # When checkpoint was created
  02_language/
    ...
```

**Resuming from a checkpoint:**
- Accumulator is restored from `accumulator.json`.
- Summaries of all prior phases are loaded into the system prompt.
- Conversation history restarts fresh from that point.
- YAML files are re-rendered from the restored accumulator.
- Later checkpoints are preserved until a change invalidates them — downstream configs are then marked "stale."

---

## Frontend

### Chat View (primary)
- Full-screen conversational interface.
- Message bubbles: user on right, agent on left.
- Agent messages can include inline YAML previews (syntax-highlighted).
- Phase indicator bar at top showing progress across all 8 phases.
- Clickable phase indicators double as checkpoint navigation — click a completed phase to roll back.
- During Phase 7, the flow graph renders as a live-updating panel embedded below messages.

### Dashboard View
- Grid of 7 config cards, one per block.
- Each card: block name, status badge (complete / draft / pending / stale), last modified.
- Click card → opens ConfigEditor.
- "Re-enter conversation" button per card drops user back into chat at the relevant phase.
- Top-level project metadata: name, domain slug, created date, current phase.

### ConfigEditor View
- Read-only YAML viewer by default (syntax-highlighted, line numbers).
- "Edit" toggle → textarea with YAML editing.
- "Validate" button runs schema validation, shows errors inline.
- "Save" writes the YAML file to disk and parses the new content back into the accumulator (reverse-sync). If the edit introduces a schema validation error, the save succeeds (file is written) but the error is shown inline and the config status changes to "stale" until fixed.
- Draft configs show a yellow banner: "This config is a draft — the block template is not yet finalized."

### Flow Graph (embedded in Chat + accessible from Dashboard)
- Directed graph: nodes = subagents, edges = routing rules.
- Node colors: start = green, terminal = red, normal = blue.
- Edges labeled with intent name.
- Updates live during Phase 7 as tool calls execute.
- Clickable nodes open a side panel showing subagent details (system prompt, valid intents, tools).

### Tech Stack
- **React** + **Vite** for build.
- **@xyflow/react** (React Flow) for graph visualization.
- **CodeMirror** + `@codemirror/lang-yaml` for YAML editing.
- **Tailwind CSS** for styling.

---

## API

```
# Project management
POST   /api/projects                                       # Create new project
GET    /api/projects                                       # List all projects
GET    /api/projects/{slug}                                # Project metadata + config statuses
DELETE /api/projects/{slug}                                # Delete project

# Conversation
POST   /api/projects/{slug}/chat                           # Send message, get response
GET    /api/projects/{slug}/history                        # Conversation history for current phase

# Checkpoints
GET    /api/projects/{slug}/checkpoints                    # List all checkpoints
POST   /api/projects/{slug}/checkpoints/{phase}/restore    # Rollback to checkpoint

# Configs
GET    /api/projects/{slug}/configs                        # All 7 configs with status
GET    /api/projects/{slug}/configs/{block}                # Single config YAML content
PUT    /api/projects/{slug}/configs/{block}                # Manual edit + reverse-sync accumulator
POST   /api/projects/{slug}/configs/validate               # Run schema validation on all

# Workflow graph
GET    /api/projects/{slug}/workflow/graph                 # Subagent nodes + routing edges

# Static frontend
GET    /                                                   # Serves the SPA
```

### Chat Response Shape

```json
{
  "reply": "Agent message text",
  "phase": "overview",
  "config_updates": [
    {"block": "agent_core", "section": "agent_workflow", "action": "update"}
  ],
  "checkpoint_created": null,
  "graph": {
    "nodes": [
      {"id": "greeting", "name": "Greeting", "type": "start"}
    ],
    "edges": [
      {"from": "greeting", "to": "profile", "intent": "consent_granted"}
    ]
  }
}
```

---

## System Prompt Strategy

### Base Prompt (always included)
Provides the agent's identity, DPG framework overview (~200 words), current project name and description, current accumulator state summary, and current phase indicator.

### Phase-Specific Additions

| Phase | Added Context |
|---|---|
| Overview | None — open-ended discovery |
| Language & Models | Available model IDs, `PreprocessingConfig` field descriptions |
| Knowledge | `KBBlocksConfig` field descriptions, glossary format, source types |
| Memory | Session schema format, graph structure pattern (subnodes, relationships) |
| Trust | `TrustConfig` field descriptions, draft status note |
| Connectors | `ConnectorsConfig` format, `ActionGatewaySettings` connector format |
| Workflow | Subagent schema, routing DSL, condensed KKB example (~100 lines), available connectors from Phase 6, intent/entity guidance |
| Review | Validation error descriptions if any |

Phase-specific schema context is auto-generated from Pydantic `Field(description=...)` metadata via a `get_schema_descriptions()` helper added to `loader.py`.

---

## Schema & Loader Updates

### schema.py

**1. Field descriptions** — Add `description` to `Field()` calls across all models so the conversation agent can auto-generate prompt context:

```python
# Before
primary_model: str

# After
primary_model: str = Field(..., description="Claude model ID for primary inference")
```

**2. Partial validation helper** — Add for validating incomplete accumulator state during the conversation:

```python
def validate_partial(block: str, data: dict) -> list[str]:
    """Validate what's present without requiring completeness.

    Returns list of validation error strings (empty = valid so far).
    Filters out 'field required' errors; returns only type/value errors.
    """
```

### loader.py

Add one new helper for prompt context generation:

```python
def get_schema_descriptions(block: str) -> dict:
    """Extract field descriptions from the Pydantic model for a given block.

    Used by the conversation agent to build phase-specific prompt context.
    Returns nested dict of {field_path: description}.
    """
```

No changes to the existing merge/validate/build pipeline.

---

## Dependencies

### Python (additions to `dev-kit/pyproject.toml`)

| Package | Purpose |
|---|---|
| `anthropic` | Claude API client |
| `fastapi` | Backend API |
| `uvicorn` | ASGI server |
| `python-dotenv` | Load `.env` for API keys |

### Frontend (new `dev-kit/frontend/package.json`)

| Package | Purpose |
|---|---|
| `react`, `react-dom` | UI framework |
| `vite` | Build tool |
| `@xyflow/react` | Graph visualization (React Flow) |
| `codemirror`, `@codemirror/lang-yaml` | YAML editor |
| `tailwindcss` | Styling |

### Environment Variables

```bash
ANTHROPIC_API_KEY=sk-ant-...   # Required
DEV_KIT_HOST=0.0.0.0           # Optional, default 0.0.0.0
DEV_KIT_PORT=8080              # Optional, default 8080
```

---

## Deployment

Single container. Multi-stage Dockerfile:

```dockerfile
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY dev-kit/frontend/ .
RUN npm ci && npm run build

FROM python:3.12-slim
WORKDIR /app
COPY dev-kit/ .
COPY --from=frontend /app/frontend/dist ./agent/static/
RUN pip install -e .
CMD ["uvicorn", "agent.app:app", "--host", "0.0.0.0", "--port", "8080"]
```

### Local Development

```bash
cd dev-kit
cd frontend && npm install && npm run build && cd ..
uv run uvicorn agent.app:app --host 0.0.0.0 --port 8080
```

---

## Out of Scope

- ASR/TTS integration in the dev-kit UI.
- Multi-user / authentication for the dev-kit service.
- Auto-generating knowledge base data files (PDFs, CSVs) — only YAML configs.
- Auto-deploying generated configs to a running DPG instance.
- Real-time collaborative editing.
