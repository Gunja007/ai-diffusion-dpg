# Memory Layer DPG

Manages all session state for the AI Composition Framework.

---

## What this service does

The Memory Layer is the single source of truth for conversation state. Agent Core reads state at the start of every turn and writes it back after the response is delivered. No other service reads or writes session state directly.

For the PoC, this is implemented as a lightweight in-process store (`InProcessSessionMemory`). The real implementation would back this with Redis or a similar persistent store — the interface is identical so the swap requires no changes to Agent Core.

State is scoped to a session TTL (default: 1 hour). After expiry, reading an unknown session returns an empty dict, which Agent Core treats as a new conversation.

---

## Folder structure

```
memory_layer/
├── main.py                 # Uvicorn entrypoint (port 8002)
├── pyproject.toml
├── config/
│   └── config.yaml         # TTL, max sessions, server port
├── src/
│   ├── session_memory.py   # InProcessSessionMemory — MemoryLayerBase implementation
│   └── server.py           # FastAPI app (all endpoints)
└── tests/
    ├── test_session_memory.py
    └── test_server.py
```

---

## HTTP API

The service runs on port **8002**.

### `POST /session/read`

Load the current state for a session. Returns an empty dict if the session does not exist or has expired.

**Request:**
```json
{ "session_id": "sess-abc123" }
```

**Response:**
```json
{
  "session_id": "sess-abc123",
  "state": {
    "turn_count": 3,
    "confirmed_entities": {"trade": "electrician", "location": "Hubli"},
    "conversation_history": [...]
  }
}
```

### `POST /session/write`

Persist updated state for a session. Overwrites any existing state for the session ID.

**Request:**
```json
{
  "session_id": "sess-abc123",
  "state": { "turn_count": 4, "confirmed_entities": {...}, "conversation_history": [...] }
}
```

**Response:** `{ "status": "ok" }`

### `GET /profile/{session_id}`

Returns a summary view of the session (turn count and confirmed entities). Equivalent to reading a subset of the full state.

**Response:**
```json
{
  "session_id": "sess-abc123",
  "turn_count": 3,
  "confirmed_entities": {"trade": "electrician"}
}
```

### `DELETE /session/{session_id}`

Removes all state for a session. Used at end-of-session or for testing cleanup.

**Response:** `{ "status": "ok" }`

### `GET /health`

Returns `{"status": "ok"}` when the service is running.

---

## State schema

Agent Core writes a dict with the following top-level keys. The Memory Layer treats state as an opaque dict — it stores and returns whatever Agent Core writes.

| Key | Type | Description |
|---|---|---|
| `turn_count` | int | Number of completed turns in this session |
| `confirmed_entities` | dict | Entities confirmed across turns (trade, location, etc.) |
| `conversation_history` | list | Alternating user/assistant message dicts |
| `workflow_step` | str | Current step in any multi-step workflow (optional) |

---

## Configuration

| Key | Description |
|---|---|
| `server.port` | HTTP port (default: 8002) |
| `memory.session_ttl_seconds` | How long to keep a session before expiry (default: 3600) |
| `memory.max_sessions` | Soft cap on concurrent in-memory sessions (default: 1000) |

---

## Running the service

```bash
source ../.venv/bin/activate
cd memory_layer
uvicorn src.server:app --port 8002
```

---

## Running tests

```bash
source ../.venv/bin/activate
cd memory_layer
pytest tests/ -v --cov=src --cov-report=term-missing
```

Expected: 30 tests, ≥87% line coverage.

---

## Dependencies

```
fastapi   >= 0.110
uvicorn   >= 0.29
pydantic  >= 2.0
pyyaml    >= 6.0
```

Requires Python 3.11+.

---

## Replacing the stub

To replace `InProcessSessionMemory` with a Redis-backed implementation:

1. Create a class that inherits from `MemoryLayerBase` (defined in `agent_core/src/interfaces/memory_layer.py`).
2. Implement `read(session_id) → dict` and `write(session_id, state) → None` with identical signatures.
3. Wire the new class into `src/server.py` — no other files need to change.
