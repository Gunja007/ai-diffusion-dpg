# Worked Outbound MCP Recipe (GH-338)

This recipe demonstrates how a DPG agent connects to another DPG agent (or any standard MCP server) as an outbound peer using the standard `McpAdapter`.

## 1. Tool Configuration

To configure the DPG agent to talk to an outbound peer agent via MCP, add the connector definition to the `tools` section of `agent_core.yaml`:

```yaml
agent_core:
  tools:
    - name: peer_query
      type: mcp
      mcp:
        # Standard MCP connection URL over SSE (or stdio if command configured)
        endpoint: "http://peer_agent_reach_mcp:8007/sse"
        api_key: "peer-secret-api-key"
        # Optional: session-level pass-through
        session_mapping:
          propagate_session_id: true
```

## 2. Python Stub Example

Here is a Python stub demonstrating how the outbound connection is initialized and how a tool call is executed using `McpAdapter` under the hood:

```python
import asyncio
from mcp import ClientSession, SseServerTransport
from agent_core.src.tools.mcp_adapter import McpAdapter

async def call_peer_agent():
    # 1. Setup connection parameters
    config = {
        "endpoint": "http://localhost:8007/sse",
        "api_key": "peer-secret-api-key"
    }
    
    # 2. Establish connection to peer MCP server
    async with SseServerTransport(f"{config['endpoint']}?api_key={config['api_key']}") as transport:
        async with ClientSession(transport.read_stream, transport.write_stream) as session:
            # Initialize the session
            await session.initialize()
            
            # 3. Discover available tools
            tools = await session.list_tools()
            print("Discovered tools:", tools)
            
            # 4. Invoke tool turn on peer agent
            result = await session.call_tool(
                name="dpg.send_message",
                arguments={
                    "session_id": "outbound-session-456",
                    "message": "Hello from caller agent!"
                }
            )
            print("Peer response:", result.content[0].text)

if __name__ == "__main__":
    asyncio.run(call_peer_agent())
```

## 3. Turn Trace

Below is a trace of the JSON-RPC messages sent over the SSE transport during an outbound tool invocation:

### A. Client Initialization
**Request (Client -> Server):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {
      "name": "dpg-caller",
      "version": "1.0.0"
    }
  }
}
```

**Response (Server -> Client):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "capabilities": {
      "tools": {}
    },
    "serverInfo": {
      "name": "dpg-mcp",
      "version": "1.0.0"
    }
  }
}
```

### B. List Tools
**Request (Client -> Server):**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list"
}
```

**Response (Server -> Client):**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [
      {
        "name": "dpg.send_message",
        "description": "Send a message to the DPG agent session.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "session_id": {
              "type": "string",
              "description": "Unique session identifier"
            },
            "message": {
              "type": "string",
              "description": "Message to send to the agent"
            },
            "locale": {
              "type": "string"
            },
            "metadata": {
              "type": "object"
            }
          },
          "required": ["session_id", "message"]
        }
      }
    ]
  }
}
```

### C. Call Tool (with Progress Notifications)
**Request (Client -> Server):**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "dpg.send_message",
    "arguments": {
      "session_id": "session-999",
      "message": "Hello"
    },
    "_meta": {
      "progressToken": "token-123"
    }
  }
}
```

**Streamed Progress Notification 1 (Server -> Client):**
```json
{
  "jsonrpc": "2.0",
  "method": "notifications/progress",
  "params": {
    "progressToken": "token-123",
    "progress": 0,
    "meta": {
      "text": "Hello, how can I help you?",
      "sentence_index": 0
    }
  }
}
```

**Final Response (Server -> Client):**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"reply\": \"Hello, how can I help you?\", \"session_id\": \"caller-agent:session-999\", \"finished\": false, \"error_type\": null, \"error_message\": null}"
      }
    ]
  }
}
```
