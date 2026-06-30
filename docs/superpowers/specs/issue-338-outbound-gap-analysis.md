# Outbound MCP Gap Analysis (GH-338)

This document analyzes the technical and architectural gaps in implementing outbound peer-agent semantics for the DPG system.

## 1. Long-Running Callbacks / Async Execution
* **Current State**: MCP tool calls are synchronous request-response loops over SSE or stdio. If a peer agent takes too long (e.g. executing a multi-step subagent path or external tools), the calling agent blocks or times out.
* **Gaps**:
  - Lack of support for asynchronous callbacks where the peer agent can immediately return a "pending" receipt and asynchronously post the results back via an out-of-band webhook or specialized MCP client callback protocol.
  - No mechanism to register webhooks dynamically per tool call.
* **Proposed Resolution**: Standardize a callback URL param in tool parameters, or implement MCP's proposed `notifications/progress` along with long timeouts and cancellation tokens.

## 2. Server Progress Tracking
* **Current State**: While the inbound channel now supports streaming sentences via MCP's `notifications/progress` events, the outbound `McpAdapter` does not yet interpret or forward progress notifications back to the caller's main SSE stream.
* **Gaps**:
  - Outbound client ignores progress notifications sent by peer servers.
  - Main Agent Core orchestrator receives outbound tool results in a block rather than a stream.
* **Proposed Resolution**: Extend the outbound `McpAdapter` to catch progress events and emit them as sub-events in the active orchestrator turn.

## 3. Peer Session Correlation
* **Current State**: Session IDs are namespaced on the server side (`caller_agent_id:session_id`).
* **Gaps**:
  - There is no automated propagation of parent-child session trace headers. If Agent A calls Agent B, Agent B starts a new session which is isolated. We cannot easily trace that Agent B's session was spawned as a direct child of Agent A's session.
* **Proposed Resolution**: Standardize a trace context header (similar to W3C Trace Context) inside the `metadata` parameter of the MCP call.

## 4. Consent Alignment
* **Current State**: Interactive consent prompts are bypassed for inbound callers based on metadata parameters or safe defaults (anonymous storage).
* **Gaps**:
  - Outbound agents have no standard mechanism to check the consent status of the user before calling a peer. If the user consented to data storage on Agent A, but Agent A invokes Agent B, Agent B has no automated way to inherit or verify that consent.
* **Proposed Resolution**: Define a standard `consent_purpose` and `user_consent` object inside the MCP metadata argument to pass verified user consent down the delegation chain.
