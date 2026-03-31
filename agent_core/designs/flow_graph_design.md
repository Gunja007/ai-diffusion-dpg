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

    valid_intents: [<intent_name>]      # NLU classifies ONLY within this set + global_intents.
                                        # Scopes intent classification to what is meaningful here.

    tools: [<tool_name>]                # Action Gateway tools available at this node.
                                        # Only these tool definitions are passed to the LLM.
                                        # Empty list = no tools exposed.

    system_prompt: |                    # Full LLM instruction block for this node.
      <text>                            # Defines: role at this stage, what to ask/say,
                                        # rules to follow, what NOT to do, tone, format.
                                        # This is the primary driver of LLM behaviour here.
                                        # Merged with agent_system_prompt at runtime.

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

  agent_system_prompt: |               # Defines the entire use case at the agent level.
    <text>                             # Covers: what this agent is, its purpose, who it serves,
                                       # what it must never do, and any hard rules that apply
                                       # unconditionally across every node and every turn.
                                       # This is the foundational instruction layer.
                                       # Channel tone and language style are injected at runtime
                                       # by the Reach Layer / Language Normalisation — not here.

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

Step 3:  Safety check on input (Trust Layer) — unchanged

Step 4:  Language Normalisation — unchanged

Step 5:  NLU Processor
         → pass current_node.valid_intents + graph.global_intents as the allowed intent set
         → NLU classifies ONLY within this scoped set

Step 6:  Determine next_node_id via routing resolution (see Routing Algorithm below)
         → entry guards expressed as routing conditions, not node-level fields
           (e.g. "don't enter evaluation unless profile_complete == true" is a
           condition on the routing rule that points to evaluation, not a
           required_inputs declaration on evaluation itself)

Step 7:  ManagerAgent assembles prompt (internal — no external call)
         → system prompt = agent_system_prompt
                         + channel/language context (from Reach Layer / Language Normalisation)
                         + current_node.system_prompt
         → messages built from current_question and session context

Step 8:  LLM call #1 with scoped tools (current_node.tools only)
         → tool set may include knowledge_retrieval (KE) if listed in node.tools

Step 9:  Tool-use loop (Manager Agent + Action Gateway)
         → if LLM calls knowledge_retrieval: orchestrator calls Knowledge Engine,
           appends retrieved chunks as tool_result, LLM continues
         → if LLM calls any other tool: routed to Action Gateway as before
         → loop continues until LLM returns a non-tool_use response

Step 10: Safety check on output (Trust Layer) — unchanged

Step 11: Return TurnResult

Step 12: [async] Write session state including next_node_id → Memory Layer
Step 13: [async] Emit turn event → Learning Layer
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

Prompt assembly is done by **ManagerAgent internally**.

The LLM receives a system prompt assembled as:

```
[agent_system_prompt]               ← from conversation_graph — use case purpose, hard rules

[channel/language context]          ← injected at runtime by Reach Layer / Language Normalisation
                                      (e.g. channel type, detected language, response format hints)

[current_node.system_prompt]        ← from the active node — what to do on this specific turn
```

`agent_system_prompt` defines the agent's mission and non-negotiable constraints. Channel and language context comes from Reach Layer and Language Normalisation — it is not hardcoded in the graph. The node system prompt governs behaviour for this specific conversation state. Node instructions take precedence where they conflict with the use-case prompt.

RAG chunks from Knowledge Engine are returned as `tool_result` in the messages array — the LLM calls `knowledge_retrieval` as a tool when it needs them. KE is never called on a fixed schedule; the LLM decides when retrieval is needed based on the user's query.

---

## Tool Scoping

Only tools listed in `current_node.tools` are passed to the LLM. This prevents the LLM from calling actions not appropriate for the current stage (e.g. `onest_apply` should never be available during profile building).

`knowledge_retrieval` is one of the available tool names. Nodes that may need RAG include it in their `tools` list; nodes that never need it (e.g. greeting, awaiting_consent) do not. The LLM calls it like any other tool — the orchestrator routes it to Knowledge Engine and returns the result as `tool_result`.

```python
# Orchestrator builds scoped tool set per turn
active_tools = [
    tool_registry.get_definition(name)
    for name in current_node.tools
]
# Passed to ManagerAgent — includes knowledge_retrieval if listed on this node
```

---

## Special Handlers

Nodes with `special_handler` bypass LLM inference entirely.

| Handler | Behaviour |
|---|---|
| `hitl` | Orchestrator returns the fixed `hitl_response` message from config. No LLM call. Triggers counsellor scheduling via Action Gateway. |
| `whatsapp_handoff` | Orchestrator triggers WhatsApp message delivery with session summary. No LLM call. |

These handlers are checked by the orchestrator before Step 7 (prompt assembly). If `current_node.special_handler` is set, skip Steps 7–9 entirely.

---

## domain.yaml Refactor Impact

This design replaces the following existing sections in domain.yaml:

| Removed | Replaced by |
|---|---|
| `conversation.workflow.steps[]` | `conversation_graph.nodes[].id` |
| `conversation.workflow.transitions{}` | `conversation_graph.nodes[].routing[]` |
| `conversation.prompt_blocks.node_instructions{}` | `conversation_graph.nodes[].system_prompt` |
| `conversation.prompt_blocks.persona` | `conversation_graph.agent_system_prompt` |

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
