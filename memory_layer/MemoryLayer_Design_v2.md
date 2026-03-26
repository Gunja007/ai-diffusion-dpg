# Memory Layer Design — v2 - will be deleted after implementation

> This is the authoritative design document for the Memory Layer redesign.
> All implementation tasks (B1, B2, A1, D1, G1–G7, K1–K8) must follow this spec exactly.

---

## 1. Core Principles

- **Memory Layer stores. It does not extract.** Agent Core (via LLM) decides what to extract from user input and calls `write()`. Memory Layer receives key/value/scope and stores it. No logic, no inference.
- **Redis is the hot path.** Every turn reads from Redis — no Neo4j in the decision loop during an active session.
- **Neo4j is the knowledge graph.** Persistent user identity, profile, journey history (branches taken, roles offered, drop-off points), and conversational signals all live in Neo4j across sessions. Raw conversation messages (user text, system text) are never stored.
- **Config-driven schema.** Declared fields come from `domain.yaml`. The Memory Layer code never hardcodes field names, node labels, or relationship types.
- **Ad-hoc data is first-class.** Anything the user says that is relevant but not in the declared config schema is stored as a child attribute node — not discarded.
- **Phone is the universal user key.** It flows from Reach Layer → Agent Core → Memory Layer via `TurnInput.phone`. The Memory Layer uses phone for all Neo4j queries. Redis is keyed by `session_id`.

---

## 2. Store Architecture

| Store | Technology | What lives here | Lifetime |
|---|---|---|---|
| Session | Redis hash, TTL-bound | Hot session state — current node, collection round, current question, signals, language, consent, loop count | Session-scoped (TTL from config) |
| Persistent | Neo4j | User identity, profile, journey history, context graph | Permanent (with consent) |

**Turn scope** is already handled by local variables inside Agent Core's `process_turn()` — `turn_id`, `current_user_message`, `normalised_input`, `nlu_result`, `rag_chunks`, `trust_input_result`. These are ephemeral Python variables that exist for one turn and are discarded after the response. No Memory Layer involvement needed. `turn_id` is generated as `uuid4()` at the start of each turn and passed to Learning Layer in `TurnEvent` for audit.

**Broadcast scope** is out of scope for this implementation.

---

## 3. Neo4j Graph Model

### 3.1 Full Structure

```
(User)
  │
  ├──[:HAS_PROFILE]──→  (UserProfile)
  │                         │
  │                         │  declared fields (from config):
  │                         │  phone, language, trade, education,
  │                         │  location, experience_years, income_urgency,
  │                         │  consent_flag, anonymous_flag
  │                         │
  │                         └──[:HAS_ATTRIBUTE]──→ (UserAttribute {
  │                                                    key, value, raw,
  │                                                    turn, journey_id
  │                                                 })
  │                                                 (one node per ad-hoc attribute)
  │
  ├──[:HAS_JOURNEY_HISTORY]──→ (JourneyHistory)    ← grouping node, no properties
  │                                  │
  │                                  └──[:JOURNEY]──→ (Journey {
  │                                                       journey_id,
  │                                                       started_at, ended_at,
  │                                                       end_reason,
  │                                                       branch_taken,
  │                                                       mental_state_at_end
  │                                                    })
  │                                                       │
  │                                                       ├──[:OFFERED]──→ (Role {
  │                                                       │                   role_id, title,
  │                                                       │                   employer,
  │                                                       │                   pay_min, pay_max,
  │                                                       │                   shortlisted,
  │                                                       │                   committed,
  │                                                       │                   follow_up_outcome
  │                                                       │                })
  │                                                       │
  │                                                       └──[:DROPPED_AT]──→ (DropOff {
  │                                                                               node,
  │                                                                               reason,
  │                                                                               timestamp
  │                                                                            })
  │
  └──[:HAS_CONTEXT]──→  (ContextGraph)             ← grouping node, no properties
                             │
                             └──[:SIGNAL]──→ (Signal {
                                                 type, turn, raw, journey_id
                                              })
                                                 │
                                                 └──[:HAS_ATTRIBUTE]──→ (ContextAttribute {
                                                                            key, value, raw,
                                                                            turn, journey_id
                                                                         })
                                                                         (one node per ad-hoc context item)
```

### 3.2 Node Descriptions

| Node | Type | Properties | Purpose |
|---|---|---|---|
| `User` | Root | (none — just label) | Anchor node. All data hangs off here. |
| `UserProfile` | Subnode | declared fields from config | Who the user is — identity + capabilities |
| `UserAttribute` | Ad-hoc child of UserProfile | key, value, raw, turn, journey_id | Anything user says about themselves not in declared fields (e.g. "allergic to sun") |
| `JourneyHistory` | Subnode | (none — grouping only) | Groups all past journeys for this user |
| `Journey` | Child of JourneyHistory | journey_id, started_at, ended_at, end_reason, branch_taken, mental_state_at_end | One node per session |
| `Role` | Child of Journey | role_id, title, employer, pay_min, pay_max, shortlisted, committed, follow_up_outcome | Job roles offered or committed to in a journey |
| `DropOff` | Child of Journey | node, reason, timestamp | Where and why the user dropped off |
| `ContextGraph` | Subnode | (none — grouping only) | Groups all conversational signals across journeys |
| `Signal` | Child of ContextGraph | type, turn, raw, journey_id | Per-turn conversational signals (objection, emotion, constraint, etc.) |
| `ContextAttribute` | Ad-hoc child of Signal | key, value, raw, turn, journey_id | Structured extraction from a signal that doesn't fit Signal.type |

### 3.3 Relationship Types

| Relationship | From → To | Meaning |
|---|---|---|
| `HAS_PROFILE` | User → UserProfile | User's identity and profile |
| `HAS_ATTRIBUTE` | UserProfile → UserAttribute | Ad-hoc profile attribute |
| `HAS_JOURNEY_HISTORY` | User → JourneyHistory | Grouping edge |
| `JOURNEY` | JourneyHistory → Journey | One journey per session |
| `OFFERED` | Journey → Role | Role offered to user this journey |
| `DROPPED_AT` | Journey → DropOff | Where user dropped off |
| `HAS_CONTEXT` | User → ContextGraph | Grouping edge |
| `SIGNAL` | ContextGraph → Signal | One conversational signal |
| `HAS_ATTRIBUTE` | Signal → ContextAttribute | Ad-hoc structured extraction from signal |

### 3.4 Pattern: Declared Fields vs Ad-hoc Attributes

The same pattern applies to all three subnodes:

- **Declared fields** — defined in `domain.yaml` config. Stored as properties directly on the subnode (UserProfile, Journey, Signal). Fast to read, indexed.
- **Ad-hoc attributes** — anything the LLM extracts that is not in the declared schema. Stored as child `*Attribute` nodes connected by `HAS_ATTRIBUTE`. Extensible, no schema change needed.

**Who writes ad-hoc attributes:** Agent Core (via LLM extraction). Memory Layer receives the key/value and creates the child node. Memory Layer never decides what is or isn't ad-hoc — it just stores what Agent Core sends.

---

## 4. Redis Schema

```
Key:   session:{session_id}
Type:  Hash
TTL:   state.session.ttl_minutes × 60 seconds  (KKB default: 60 min)

Fields (all stored as strings — type coercion on read):
  user_phone         "9876543210"
  journey_id         "j1"                         # = session_id
  current_node       "market_truth"               # workflow step
  collection_round   "2"
  current_question   "aapke liye kaunsi jobs..."
  market_signal      "strong"                     # strong | weak | absent
  skill_match        "partial"                    # direct | partial | gap
  income_urgency     "high"
  language           "hindi"
  is_returning       "true"
  consent            "true"
  loop_count         "0"
  mental_state       "orientation"
  options_presented  "[]"                         # JSON-encoded list
```

**On first turn (new session_id):**
1. `redis.exists(f"session:{session_id}")` → false
2. Check Neo4j for `phone` → determines `is_returning`
3. Create Journey node in Neo4j under JourneyHistory
4. `redis.hset(f"session:{session_id}", mapping=initial_state)`
5. `redis.expire(f"session:{session_id}", ttl_seconds)`

**On subsequent turns:**
- `redis.hgetall(f"session:{session_id}")` — full hash read, O(1)

**On paused session resume (Redis expired, user reconnects):**
- New `session_id` → session init flow runs again
- `is_returning=true` → prior journey summary loaded from Neo4j into `ContextBundle.journey`

---

## 5. Interface — MemoryLayerBase

```python
"""
agent_core/src/interfaces/memory_layer.py

The only API Agent Core ever calls. Four methods.
Agent Core never touches Redis or Neo4j directly.
"""

from abc import ABC, abstractmethod
from typing import Any
from src.models import ContextBundle


class MemoryLayerBase(ABC):

    @abstractmethod
    def context_bundle(self, session_id: str, phone: str) -> ContextBundle:
        """
        Called at the START of every turn.

        First call for a new session_id:
          - Checks Neo4j for phone (returning user?)
          - Creates Journey node in Neo4j
          - Initialises Redis hash with default session state
          - Loads prior journey summary if returning user

        All subsequent calls:
          - Reads Redis hash (hot path — no Neo4j)
          - Reads UserProfile from Neo4j (declared fields + UserAttribute nodes)
          - Returns ContextBundle

        Returns empty ContextBundle on any failure. Never raises.
        """

    @abstractmethod
    def write(self, session_id: str, phone: str, scope: str, key: str, value: Any) -> None:
        """
        Called AFTER every turn (async daemon thread).

        scope="session"
          → Redis HSET on session:{session_id} + reset TTL

        scope="persistent"
          → Neo4j MERGE on the correct node, resolved from entity_map config
          → If key matches a declared field: write as property on the node
          → If key does not match any declared field: create/update UserAttribute child node

        scope="signal"
          → Create Signal node under ContextGraph
          → If structured extraction present: create ContextAttribute child nodes

        scope="journey_event"
          → Create/update Role or DropOff node under current Journey

        Never raises.
        """

    @abstractmethod
    def flush_session(self, session_id: str, phone: str, end_reason: str) -> None:
        """
        Called when a session ends:
          - termination_intent detected by NLU
          - SIGTERM received
          - Escalation to HITL

        Operations (in order):
          1. Read full Redis hash
          2. Execute merge_on_session_end rules from config
             → promotes session fields to Neo4j persistent nodes
          3. Close Journey node: SET ended_at, end_reason, mental_state_at_end
          4. If consent=false:
             MATCH (u:User {phone: $phone}) DETACH DELETE u
             (erases all graph data for this user)
          5. DELETE Redis key

        Never raises.
        """

    @abstractmethod
    def delete_user(self, phone: str) -> None:
        """
        DPDP right-to-erasure.
        MATCH (u:User {phone: $phone}) DETACH DELETE u
        Removes User + all subnodes (UserProfile, JourneyHistory, ContextGraph)
        and all their descendants via CASCADE.
        Never raises.
        """
```

---

## 6. Data Contracts

### 6.1 ContextBundle

ContextBundle is the **single object that `context_bundle()` returns to Agent Core at the start of every turn.**
Instead of Agent Core making separate calls to Redis and Neo4j itself, Memory Layer assembles everything into
one package and hands it over. Think of it as: *"everything the LLM needs to know about this user right now."*

Three fields, three different sources, three different time horizons:

| Field | Source | Time horizon | What it represents |
|---|---|---|---|
| `session` | Redis hash | This session only | What's happening right now — current node, question, signals, language |
| `profile` | Neo4j UserProfile | Across all sessions | Who this user is — trade, location, constraints, ad-hoc attributes |
| `journey` | Neo4j JourneyHistory | Last session only | What happened last time — roles offered, outcome, drop-off point |

**Why one object instead of separate calls:**
Without ContextBundle, Agent Core would have to call Redis directly, call Neo4j directly for profile,
call Neo4j again for journey summary, and know how to parse each store's response format. That violates
the design rule — Memory Layer stores, Agent Core orchestrates. Agent Core must not know anything about
Redis or Neo4j internals. `context_bundle()` is the single read boundary.

```python
# agent_core/src/models.py
# Replaces SessionState everywhere in Agent Core

@dataclass
class ContextBundle:
    session: dict         # full Redis hash — current session state
                          # keys: current_node, collection_round, current_question,
                          #       market_signal, skill_match, income_urgency,
                          #       language, is_returning, consent, loop_count

    profile: dict         # UserProfile declared fields + all UserAttribute nodes
                          # {
                          #   "trade": "electrician",
                          #   "education": "iti",
                          #   "location": "hubli",
                          #   ...declared fields...,
                          #   "attributes": [
                          #     {"key": "sun_sensitivity", "value": "cannot_work_outdoors",
                          #      "raw": "i am allergic to sun"}
                          #   ]
                          # }

    journey: dict | None  # Prior journey summary — only for returning users
                          # {
                          #   "roles_offered": [...],
                          #   "committed_to": {...} | None,
                          #   "last_outcome": "employer_ghost" | None,
                          #   "drop_off_node": "skill_evaluation_a" | None,
                          #   "signals": [{"type": "pay_objection", "raw": "..."}]
                          # }
                          # None for new users
```

### 6.2 How Agent Core Uses ContextBundle

| Step | Field used | Purpose |
|---|---|---|
| Workflow routing | `bundle.session["current_node"]` | Which branch to take |
| Consent check | `bundle.session["consent"]` | Gate for profile collection |
| NLU context | `bundle.session["current_question"]` + `bundle.session["current_node"]` | Resolve ambiguous intents |
| System prompt | `bundle.profile` + `bundle.session["current_node"]` + `bundle.session["current_question"]` | Build LLM context |
| Language | `bundle.session["language"]` | Respond in user's language |
| KE retrieve | `bundle.profile["trade"]` + `bundle.profile["location"]` | Entity filters for RAG |
| Re-entry | `bundle.journey` | Brief LLM on prior context for returning user |
| HITL threshold | `bundle.session["loop_count"]` | Trigger counsellor if loop_count >= threshold |

---

## 7. Config Schema

**File:** `memory_layer/config/domain.yaml`

```yaml
state:
  session:
    ttl_minutes: 60
    schema:
      current_node:      { type: string,  default: "awaiting_consent" }
      mental_state:      { type: enum,    values: [fog, orientation, evaluation, commitment, follow_through] }
      market_signal:     { type: enum,    values: [strong, weak, absent] }
      skill_match:       { type: enum,    values: [direct, partial, gap] }
      income_urgency:    { type: enum,    values: [immediate, short_term, flexible] }
      collection_round:  { type: int,     default: 0 }
      current_question:  { type: string,  default: "" }
      options_presented: { type: list,    default: [] }
      loop_count:        { type: int,     default: 0 }
      consent:           { type: bool,    default: false }

  persistent:
    backend: neo4j
    graph:
      user_node:
        label: User
        key: phone

      subnodes:
        UserProfile:
          rel: HAS_PROFILE
          declared_fields:
            - phone
            - language
            - trade
            - education
            - location
            - experience_years
            - income_urgency
            - consent_flag
            - anonymous_flag
          adhoc:
            label: UserAttribute
            rel: HAS_ATTRIBUTE
            fields: [key, value, raw, turn, journey_id]

        JourneyHistory:
          rel: HAS_JOURNEY_HISTORY
          grouping: true            # no properties on this node
          child:
            label: Journey
            rel: JOURNEY
            fields: [journey_id, started_at, ended_at, end_reason, branch_taken, mental_state_at_end]
            children:
              - label: Role
                rel: OFFERED
                fields: [role_id, title, employer, pay_min, pay_max, shortlisted, committed, follow_up_outcome]
              - label: DropOff
                rel: DROPPED_AT
                fields: [node, reason, timestamp]

        ContextGraph:
          rel: HAS_CONTEXT
          grouping: true            # no properties on this node
          child:
            label: Signal
            rel: SIGNAL
            fields: [type, turn, raw, journey_id]
            adhoc:
              label: ContextAttribute
              rel: HAS_ATTRIBUTE
              fields: [key, value, raw, turn, journey_id]

    merge_on_session_end:
      # Promotes session fields to persistent on flush_session()
      - session_field: mental_state
        target: Journey.mental_state_at_end
      - session_field: market_signal
        target: Journey.branch_taken        # recorded on the Journey node
      - session_field: options_presented
        target: Role                        # creates OFFERED edges for each role_id in the list

  reengagement:
    triggers:
      - event: DOP_MT              # drop-off after market truth
        delay_hours: 72
        channel: outbound_call
        message_template: kkb_reengagement_mt
      - event: DOP_EG              # employer ghost
        delay_hours: 72
        channel: outbound_call
        message_template: kkb_employer_ghost
      - event: DOP_RL              # evaluation loop
        loop_threshold: 3
        action: hitl_counsellor
```

**SCOPE_MAP** — compiled at boot from config, used by `write()` to route fields:

```python
# Built once at startup from domain.yaml
# Agent Core uses this to determine scope before calling write()
SCOPE_MAP = {
    # session fields (Redis)
    "current_node":      "session",
    "mental_state":      "session",
    "market_signal":     "session",
    "skill_match":       "session",
    "income_urgency":    "session",
    "collection_round":  "session",
    "current_question":  "session",
    "options_presented": "session",
    "loop_count":        "session",
    "consent":           "session",

    # persistent fields — declared (Neo4j UserProfile)
    "trade":             "persistent",
    "education":         "persistent",
    "location":          "persistent",
    "experience_years":  "persistent",
    "language":          "persistent",
    "anonymous_flag":    "persistent",

    # signal (Neo4j ContextGraph)
    "signal":            "signal",

    # journey events (Neo4j JourneyHistory)
    "role_offered":      "journey_event",
    "drop_off":          "journey_event",
}
# Any key NOT in SCOPE_MAP → treated as ad-hoc → scope="persistent", stored as UserAttribute
```

---

## 8. Operation Flows

### 8.1 New User — First Turn

```
1. Agent Core calls context_bundle(session_id, phone)
2. Memory Layer: redis.exists("session:{session_id}") → false
3. Memory Layer: neo4j.find_user(phone) → None  (new user)
4. Memory Layer: neo4j.create_user_graph(phone)
   → CREATE (u:User)
   → CREATE (u)-[:HAS_PROFILE]->(up:UserProfile {phone})
   → CREATE (u)-[:HAS_JOURNEY_HISTORY]->(jh:JourneyHistory)
   → CREATE (u)-[:HAS_CONTEXT]->(cg:ContextGraph)
5. Memory Layer: neo4j.create_journey(phone, journey_id)
   → MATCH (jh:JourneyHistory)<-[:HAS_JOURNEY_HISTORY]-(u:User {phone})
   → CREATE (jh)-[:JOURNEY]->(j:Journey {journey_id, started_at})
6. Memory Layer: redis.hset("session:{session_id}", initial_state)
   → current_node="awaiting_consent", consent="false", is_returning="false", ...
7. Memory Layer: redis.expire("session:{session_id}", ttl)
8. Returns ContextBundle(session=initial_state, profile={phone: phone}, journey=None)
```

### 8.2 Returning User — Session Resumes

```
1. Agent Core calls context_bundle(session_id, phone)
2. Memory Layer: redis.exists → false (new session_id for new session)
3. Memory Layer: neo4j.find_user(phone) → User node found
4. Memory Layer: neo4j.create_journey(phone, journey_id)  (new Journey node)
5. Memory Layer: neo4j.get_profile(phone)
   → reads UserProfile declared fields + all UserAttribute nodes
6. Memory Layer: neo4j.get_last_journey_summary(phone)
   → reads last Journey + its Role + DropOff + Signal nodes
   → builds journey summary dict
7. Memory Layer: redis.hset with profile data pre-populated, is_returning="true"
8. Returns ContextBundle(
       session={..., is_returning: "true"},
       profile={trade: "electrician", ...attributes: [...]},
       journey={roles_offered: [...], drop_off_node: "...", signals: [...]}
   )
```

### 8.3 After Every Turn (Async Write)

```
Agent Core (daemon thread):
  for key, value in state_updates.items():
      scope = SCOPE_MAP.get(key, "persistent")   # unknown keys → persistent as UserAttribute
      memory.write(session_id, phone, scope, key, value)

Memory Layer:
  scope="session"      → redis.hset("session:{session_id}", key, value) + reset TTL
  scope="persistent"   → neo4j.upsert_profile_field(phone, key, value)
                          if key in declared_fields: SET up.{key} = $value
                          else: MERGE UserAttribute node
  scope="signal"       → neo4j.create_signal(phone, journey_id, value)
  scope="journey_event"→ neo4j.create_journey_child(phone, journey_id, key, value)
```

### 8.4 Session End (flush_session)

```
Agent Core calls flush_session(session_id, phone, end_reason)
  when: nlu_result.intent == "termination_intent"
     or: was_escalated == True
     or: SIGTERM received

Memory Layer:
  1. session = redis.hgetall("session:{session_id}")
  2. For each rule in merge_on_session_end config:
       val = session.get(rule["session_field"])
       if val: neo4j.upsert_path(phone, journey_id, rule["target"], val)
  3. neo4j.close_journey(phone, journey_id, end_reason, mental_state)
       → MATCH Journey {journey_id} SET ended_at=$now, end_reason=$reason
  4. if session["consent"] == "false":
       neo4j.delete_user(phone)
       → MATCH (u:User {phone: $phone}) DETACH DELETE u
  5. redis.delete("session:{session_id}")
```

### 8.5 DPDP Erasure

```
Agent Core calls memory.delete_user(phone)

Memory Layer:
  MATCH (u:User {phone: $phone}) DETACH DELETE u
  → Cascades through all subnodes and their descendants
```

---

## 9. Implementation Components

| Task | Component | File | What it does |
|---|---|---|---|
| A1 | `ContextBundle` dataclass | `agent_core/src/models.py` | Replaces `SessionState` |
| B1 | `MemoryLayerBase` (4 methods) | `agent_core/src/interfaces/memory_layer.py` | New interface |
| K1 | `RedisSessionStore` | `memory_layer/src/session_store.py` | HSET/HGETALL/DELETE/EXPIRE |
| K2 | `Neo4jUserStore` | `memory_layer/src/neo4j_user_store.py` | CREATE user graph, get/upsert UserProfile + UserAttribute |
| K3 | `Neo4jJourneyStore` | `memory_layer/src/neo4j_journey_store.py` | CREATE/CLOSE Journey, get last journey summary, Role/DropOff nodes |
| K4 | `Neo4jContextStore` | `memory_layer/src/neo4j_context_store.py` | CREATE Signal + ContextAttribute nodes |
| K5 | `MemoryLayer` | `memory_layer/src/memory_layer.py` | Implements MemoryLayerBase. Orchestrates K1–K4. |
| K6 | `domain.yaml` | `memory_layer/config/domain.yaml` | Full KKB config (schema above) |
| K7 | `MemoryLayerServer` | `memory_layer/src/server.py` | FastAPI — 4 endpoints mirroring the 4 methods |
| K8 | `ReengagementEngine` | `memory_layer/src/reengagement.py` | Reads triggers from config, fires via Reach Layer |
| D1 | `HttpMemoryClient` | `agent_core/src/http_clients/memory_layer.py` | HTTP client implementing MemoryLayerBase |
| G1 | Orchestrator — turn start | `agent_core/src/orchestrator.py` | Call `context_bundle()` replacing `read_session()` |
| G2 | Orchestrator — routing | `agent_core/src/orchestrator.py` | Workflow routing from `bundle.session["current_node"]` |
| G3 | Orchestrator — prompt | `agent_core/src/orchestrator.py` | Pass `bundle.profile` + `bundle.session` to `build_system_prompt()` |
| G4 | Orchestrator — messages | `agent_core/src/orchestrator.py` | `build_messages()` — no history, RAG chunks only |
| G5 | Orchestrator — async write | `agent_core/src/orchestrator.py` | Async `write()` loop replacing `write_session()` |
| G6 | Orchestrator — session end | `agent_core/src/orchestrator.py` | `flush_session()` on termination/escalation |
| G7 | Orchestrator — HITL | `agent_core/src/orchestrator.py` | Bypass LLM when `loop_count >= threshold` from config |

---

## 10. FastAPI Endpoints (Memory Layer Server — port 8002)

| Method | Endpoint | Calls | Request | Response |
|---|---|---|---|---|
| POST | `/context_bundle` | `memory.context_bundle()` | `{session_id, phone}` | `ContextBundle` as JSON |
| POST | `/write` | `memory.write()` | `{session_id, phone, scope, key, value}` | `{status: "ok"}` |
| POST | `/flush_session` | `memory.flush_session()` | `{session_id, phone, end_reason}` | `{status: "ok"}` |
| DELETE | `/user/{phone}` | `memory.delete_user()` | — | `{status: "ok"}` |
| GET | `/health` | — | — | `{status: "ok"}` |

---

## 11. Responsibility Split

| Layer | Responsibility |
|---|---|
| Agent Core (LLM) | Extracts declared fields + ad-hoc attributes from user input |
| Agent Core (Orchestrator) | Resolves scope from SCOPE_MAP, calls `write()` per field |
| Memory Layer | Receives key/value/scope, stores in Redis or Neo4j. No extraction logic. |
| ReengagementEngine | Reads triggers from config, fires outbound calls via Reach Layer |

---

## 12. Next Track — After Memory Layer Is Complete

**KGExternal — Knowledge Engine market knowledge graph:**

This will be implemented as a separate track immediately after the Memory Layer is done.

- Neo4j for structured ONEST market data: `(Job)-[:REQUIRES_TRADE]→(Trade)`, `(Job)-[:LOCATED_IN]→(District)`
- ChromaDB stays for document RAG (labour schemes, trade descriptions)
- KE retrieval uses both: structured Neo4j query + semantic ChromaDB search, results merged before returning chunks
- This is a KE change — does not affect the Memory Layer interface or implementation
- Requires a separate design pass before implementation begins

**Broadcast scope** — pub/sub fan-out. No dependency from current design. Defer indefinitely.
