# Generic Flow Graph Engine — Agent Core Design

Version 1.0 · March 2026

---

## Overview

The Agent Core becomes graph-aware. Any usecase deployer defines their conversation as a directed graph of nodes. The engine loads this graph at startup and traverses it turn by turn — determining where the user is, what intents are valid, and where to go next — entirely driven by config.

This replaces the flat `WorkflowStep` enum and implicit LLM-driven routing with an explicit, declarative graph model.

---

## Core Concepts

| Concept | Description |
|---|---|
| `ConversationGraph` | The full usecase graph — a collection of nodes and global rules |
| `GraphNode` | A single state in the conversation. Owns its own intents, routing, tools, and LLM system prompt |
| `current_node_id` | Stored in session state. The only piece of graph state the Memory Layer holds |
| Intent-scoped NLU | NLU classifies only within the valid intents of the current node + global intents |
| Routing | Deterministic: intent + optional session field conditions → next_node_id |

---

## GraphNode Schema

Each node is a self-contained unit. Everything the Agent Core, NLU, and LLM need for a given conversation state lives inside the node.

```yaml
nodes:
  - id: <string>                        # Unique identifier. Used as current_node_id in session.
    name: <string>                      # Human-readable label.
    description: <string>               # What this node represents in the conversation.

    is_start: <bool>                    # Exactly one node per graph must be true.
    is_terminal: <bool>                 # No routing rules required. Session ends here.
    special_handler: <string|null>      # Optional. Bypasses LLM entirely.
                                        # Values: "hitl" | "whatsapp_handoff" | null

    required_inputs: [<field_name>]     # Session fields that MUST be populated before
                                        # entering this node. Guard enforced by orchestrator.

    outputs: [<field_name>]             # Fields this node is expected to produce/confirm
                                        # in session state by the end of the turn.

    valid_intents: [<intent_name>]      # NLU classifies ONLY within this set + global_intents.
                                        # Scopes intent classification to what is meaningful here.

    tools: [<tool_name>]                # Action Gateway tools available at this node.
                                        # Only these tool definitions are passed to the LLM.
                                        # Empty list = no tools exposed.

    system_prompt: |                    # Full LLM instruction block for this node.
      <text>                            # Defines: role at this stage, what to ask/say,
                                        # rules to follow, what NOT to do, tone, format.
                                        # This is the primary driver of LLM behaviour here.
                                        # Merged with global persona at runtime.

    routing:                            # Ordered list of routing rules. First match wins.
      - intent: <intent_name>
        next_node: <node_id>

      - intent: <intent_name>           # Conditional routing: evaluated against session fields.
        condition:
          field: <session_field_name>
          operator: <eq|in|lt|gt|not_eq>
          value: <value_or_list>
        next_node: <node_id>

      - intent: <intent_name>           # Multiple conditions (all must be true).
        conditions:
          - field: <field>
            operator: <op>
            value: <value>
          - field: <field>
            operator: <op>
            value: <value>
        next_node: <node_id>

      - intent: "*"                     # Catch-all for this node. Stays on same node.
        next_node: <node_id>
```

---

## ConversationGraph Schema

The top-level config block that wraps all nodes and defines graph-wide rules.

```yaml
conversation_graph:
  graph_id: <string>                    # Usecase identifier (e.g. "kkb_iti_graduate")
  version: <string>                     # Semver. Logged with every turn for auditability.

  global_persona: |                     # Injected into every node's system prompt.
    <text>                              # Defines the agent identity, brand voice, tone.
                                        # Never overrides node-level instructions.

  global_intents: [<intent_name>]       # Intents valid at ANY node regardless of current node.
                                        # Examples: counsellor_request, termination_intent,
                                        # whatsapp_handoff_request.
                                        # These are ALWAYS added to NLU's classification set.

  global_routing:                       # Routing rules for global_intents. Applied after
                                        # node-level routing fails to match.
    - intent: <intent_name>
      next_node: <node_id>
    - intent: <intent_name>
      condition:
        field: <field>
        operator: <op>
        value: <value>
      next_node: <node_id>

  default_fallback_node: <node_id>      # Where to go on unknown or unmatched intent.
                                        # Typically a re-prompt or clarification node.

  nodes:
    - <GraphNode>
    - <GraphNode>
    ...
```

---

## How the Orchestrator Uses the Graph

### Startup

1. Load `conversation_graph` block from domain config YAML.
2. Parse into `ConversationGraph` object. Validate: exactly one `is_start` node, all `next_node` references resolve, no orphaned nodes.
3. Pre-compute NLU intent set per node: merge node.valid_intents + graph.global_intents for every
   node and store as a dict keyed by node_id. This is static after startup — computed once,
   looked up by node_id on every turn instead of recomputing the merge each time.
   Example: { "market_truth": ["interested_engaged", "pay_disappointment", ..., "counsellor_request"] }

4. Pre-compute tool definition set per node: for each node, fetch the full tool definitions from
   Action Gateway for only the tools listed in node.tools. Store as a dict keyed by node_id.
   At turn time, the active tool list is a single dict lookup — no registry filtering per request.
   Example: { "commitment": [<onest_apply_def>, <counsellor_schedule_def>], "greeting": [] }

   Both structures are stored as class-level attributes on AgentCore (not per-session).
   They are safe for concurrent request access because they are read-only after startup.

### Per-Turn Execution (modifications to existing 12-step sequence)

```
Step 1:  Read session state (Memory Layer)
         → extract current_node_id from session
         → if no current_node_id: use graph.start_node

Step 2:  Resolve current_node = graph.nodes[current_node_id]

Step 3:  Check required_inputs guard
         → if any field in current_node.required_inputs is absent from session:
           route to the node that collects those fields (configured as fallback)

Step 4:  Safety check on input (Trust Layer) — unchanged

Step 5:  Language Normalisation — unchanged

Step 6:  NLU Processor
         → pass current_node.valid_intents + graph.global_intents as the allowed intent set
         → NLU classifies ONLY within this scoped set

Step 7:  Determine next_node_id via routing resolution (see Routing Algorithm below)

Step 8:  ManagerAgent assembles prompt (internal — no external call)
         → system prompt = global_persona + current_node.system_prompt
         → messages built from current_question and session context

Step 8b: [conditional] RAG retrieval (Knowledge Engine)
         → called only when relevant knowledge is needed for this turn
           (e.g. scheme info, trade descriptions, training institutes)
         → NOT called on every turn
         → retrieved chunks appended to messages if called

Step 9:  LLM call #1 with scoped tools (current_node.tools only)

Step 10: Tool-use loop (Manager Agent + Action Gateway) — unchanged

Step 11: Safety check on output (Trust Layer) — unchanged

Step 12: Return TurnResult

Step 13: [async] Write session state including next_node_id → Memory Layer
Step 14: [async] Emit turn event → Learning Layer
```

---

## Routing Algorithm

```
function resolve_next_node(current_node, nlu_result, session_state):

  # 1. Try node-level routing rules (ordered, first match wins)
  for rule in current_node.routing:
    if rule.intent == nlu_result.intent OR rule.intent == "*":
      if rule has no condition:
        return rule.next_node
      if evaluate_condition(rule.condition, session_state):
        return rule.next_node

  # 2. Try global routing rules
  for rule in graph.global_routing:
    if rule.intent == nlu_result.intent:
      if rule has no condition:
        return rule.next_node
      if evaluate_condition(rule.condition, session_state):
        return rule.next_node

  # 3. Fallback
  return graph.default_fallback_node

function evaluate_condition(condition, session_state):
  value = session_state.get(condition.field)
  match condition.operator:
    "eq"     → return value == condition.value
    "not_eq" → return value != condition.value
    "in"     → return value in condition.value   # condition.value is a list
    "lt"     → return value < condition.value
    "gt"     → return value > condition.value
```

---

## Session State Changes

`current_node_id` is the single new field the Memory Layer must persist. Everything else (profile fields, confirmed entities, session history) remains as-is.

### Consent-Declined Profile Handling

If a user declines consent, the conversation still proceeds through profile-building nodes — those profile fields are needed to serve the user during the session. However, when the session ends (terminal node reached or `flush_session` called), the orchestrator checks `consent_status`:

```
on session end:
  if session.consent_status == "declined":
    Memory Layer: delete all profile fields collected this session
    Memory Layer: do NOT write to User Profile Store
    Only session-scoped (in-flight) data is flushed — nothing persists
```

This is enforced by the orchestrator at `flush_session` time, not by individual nodes. Nodes do not need to be aware of consent status during collection — they collect normally. The deletion happens once, at the end.

```
session state (key additions):
  current_node_id: <node_id>         # Updated at end of every turn
  node_entry_count: dict             # { node_id: int } — how many turns spent in each node
                                     # Used for HITL loop detection, drop-off triggers
```

`node_entry_count` is incremented by the orchestrator each time a node is entered. This replaces the current `loop_count` mechanism and makes it node-specific rather than session-global.

---

## NLU Scoping

The NLU processor receives the scoped intent set instead of the full global intent list:

```python
# Before (flat list)
nlu_intents = config.preprocessing.nlu.intents  # all intents, always

# After (scoped)
nlu_intents = current_node.valid_intents + graph.global_intents
```

This improves classification accuracy: NLU is not asked to distinguish between intents that are not meaningful at the current stage.

---

## System Prompt Assembly

Prompt assembly is done by **ManagerAgent internally** — not by the Knowledge Engine. KE is only invoked when RAG retrieval is needed for the turn.

The LLM receives a system prompt assembled as:

```
[global_persona]

[current_node.system_prompt]
```

The global persona establishes identity and tone. The node system prompt defines exactly what the LLM should do, ask, avoid, and produce at this stage. Node instructions take precedence where they conflict.

RAG chunks from KE (when retrieved) are appended to the messages array, not to the system prompt.

---

## Tool Scoping

Only tools listed in `current_node.tools` are passed to the LLM. This prevents the LLM from calling actions not appropriate for the current stage (e.g. `onest_apply` should never be available during profile building).

```python
# Orchestrator builds scoped tool set per turn
active_tools = [
    tool_registry.get_definition(name)
    for name in current_node.tools
]
# Passed to ManagerAgent and Knowledge Engine
```

---

## Special Handlers

Nodes with `special_handler` bypass LLM inference entirely.

| Handler | Behaviour |
|---|---|
| `hitl` | Orchestrator returns the fixed `hitl_response` message from config. No LLM call. Triggers counsellor scheduling via Action Gateway. |
| `whatsapp_handoff` | Orchestrator triggers WhatsApp message delivery with session summary. No LLM call. |

These handlers are checked by the orchestrator before Step 8 (prompt assembly). If `current_node.special_handler` is set, skip Steps 8–10 entirely.

---

## domain.yaml Refactor Impact

This design replaces the following existing sections in domain.yaml:

| Removed | Replaced by |
|---|---|
| `conversation.workflow.steps[]` | `conversation_graph.nodes[].id` |
| `conversation.workflow.transitions{}` | `conversation_graph.nodes[].routing[]` |
| `conversation.prompt_blocks.node_instructions{}` | `conversation_graph.nodes[].system_prompt` |
| `conversation.prompt_blocks.persona` | `conversation_graph.global_persona` |

Sections that remain unchanged: `agent`, `preprocessing`, `connectors`, `trust`, `knowledge`, `hitl`, `messages`.

---

## Validation Rules (enforced at startup)

1. Exactly one node has `is_start: true`.
2. Every `next_node` reference in routing rules resolves to a node id in the graph.
3. Every tool name in `node.tools` must exist in the Action Gateway tool registry.
4. Every intent in `node.valid_intents` must exist in `preprocessing.nlu.intents`.
5. Global intents must NOT appear in any node's `valid_intents` (they're added automatically).
6. Terminal nodes have no routing rules.
7. At least one routing rule per non-terminal node (or a catch-all `"*"` rule).
