# Trust Layer DPG

Mandatory safety and compliance gate. Every input and every output passes through this layer.

---

## What this service does

The Trust Layer enforces content safety, consent, and escalation rules on every turn. Agent Core calls it twice per turn — once on the raw user input (before the LLM sees it) and once on the LLM output (before it reaches the user). Neither check is skippable.

For the PoC, this is `BasicTrustLayer`: phrase-based matching with no ML model. The interface is identical to what a production ML-backed guardrails engine would implement.

**Fail-open behaviour:** If the Trust Layer service is unreachable, Agent Core's HTTP client returns a default "allow" result rather than blocking the turn. This prevents the Trust Layer from becoming a hard dependency that crashes the conversation. Production deployments should change this to fail-closed.

---

## Folder structure

```
trust_layer/
├── main.py                 # Uvicorn entrypoint (port 8003)
├── pyproject.toml
├── config/
│   └── config.yaml         # Blocked phrases, escalation topics, output rules
├── src/
│   ├── guardrails.py       # BasicTrustLayer — TrustLayerBase implementation
│   └── server.py           # FastAPI app (all endpoints)
└── tests/
    ├── test_guardrails.py
    └── test_server.py
```

---

## HTTP API

The service runs on port **8003**.

### `POST /check/input`

Check a user message before it reaches the LLM.

**Request:**
```json
{ "session_id": "sess-abc123", "message": "electrician ka kaam chahiye" }
```

**Response — allowed:**
```json
{ "passed": true, "action": "allow", "reason": null }
```

**Response — blocked:**
```json
{ "passed": false, "action": "block", "reason": "Input contains blocked phrase: 'bomb'" }
```

**Response — escalate to human:**
```json
{ "passed": false, "action": "escalate", "reason": "Escalation topic detected: 'suicide'" }
```

### `POST /check/output`

Check the LLM-generated response before it reaches the user.

**Request:**
```json
{ "session_id": "sess-abc123", "response": "The salary range for electricians is ₹15,000–₹28,000/month." }
```

**Response:**
```json
{ "passed": true, "action": "allow", "reason": null }
```

If blocked, Agent Core replaces the response with the configured `output_blocked_message`.

### `POST /check/consent`

Check whether a user has granted consent for a specific write or identity connector action.

**Request:**
```json
{ "session_id": "sess-abc123", "connector_name": "onest_apply" }
```

**Response:**
```json
{ "granted": true, "reason": null }
```

For the PoC stub, consent is always granted for any connector not in the blocked list.

### `GET /health`

Returns `{"status": "ok"}` when the service is running.

---

## Rules (configured in YAML)

All rules are loaded from `config/config.yaml` at startup. Nothing is hardcoded.

### Input rules

| Rule type | Config key | Behaviour |
|---|---|---|
| Blocked phrases | `trust.input_rules.blocked_phrases` | Case-insensitive substring match. Returns `action: block`. |
| Escalation topics | `trust.input_rules.escalation_topics` | Case-insensitive substring match. Returns `action: escalate`. |

Current PoC blocked phrases: `bomb`, `weapon`, `kill`, `threat`, `violence`

Current PoC escalation topics: `arrested`, `police case`, `court notice`, `FIR`, `jail`, `suicide`

### Output rules

| Rule type | Config key | Behaviour |
|---|---|---|
| Blocked phrases | `trust.output_rules.blocked_phrases` | Blocks LLM responses containing these strings. |

Current PoC output blocked phrases: `cannot help`, `as an AI I`, `guaranteed placement`, `100% job guarantee`

---

## Configuration

| Key | Description |
|---|---|
| `server.port` | HTTP port (default: 8003) |
| `trust.input_rules.blocked_phrases` | List of phrases that block the input entirely |
| `trust.input_rules.escalation_topics` | List of phrases that trigger human handoff |
| `trust.output_rules.blocked_phrases` | List of phrases that block the LLM output |

---

## Running the service

```bash
source ../.venv/bin/activate
cd trust_layer
uvicorn src.server:app --port 8003
```

---

## Running tests

```bash
source ../.venv/bin/activate
cd trust_layer
pytest tests/ -v --cov=src --cov-report=term-missing
```

Expected: 39 tests, 100% line coverage.

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

To replace `BasicTrustLayer` with an ML-backed guardrails engine:

1. Create a class that inherits from `TrustLayerBase` (defined in `agent_core/src/interfaces/trust_layer.py`).
2. Implement `check_input`, `check_output`, and `check_consent` with identical signatures and return shapes.
3. Wire the new class into `src/server.py` — no other files need to change.
