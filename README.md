# Governance SDK

A lightweight, non-blocking, plug-and-play Python SDK to automatically intercept, evaluate, and authorize tool calls in **LangChain**, **LangGraph**, and **CrewAI** agentic applications.

---

## Features

- **Multi-Framework Auto-Interception**: Automatically intercepts tool calls in **LangChain**, **LangGraph**, and **CrewAI** (by hooking base tool run/arun methods) with zero configuration.
- **Zero-Touch Automatic Context Capture**: Walks up the Python execution stack frames to auto-resolve active context details such as `session_id`, `agent_name`, `purpose`, and `trace_id` by inspecting local variables. No manual wrapping required!
- **Three-Tier Governance Workflow**: Evaluates risk and routes tool execution decisions:
  - `allow`: Automatically executes the tool.
  - `preview_and_confirmation`: Triggers an interactive CLI prompt to confirm execution.
  - `needs_full_review`: Triggers an interactive CLI prompt indicating full review is required.
- **Local Policy Fallback Engine**: If the remote governance server is unreachable, the SDK falls back to a deterministic rules-based local safety scanner (detecting destructive commands, SQL injections, credentials, and sensitive file paths) to dynamically decide whether to allow, preview, or block the action.
- **Exception-Based Control**: If a developer/user denies an execution prompt, the SDK raises `PermissionDeniedError` or `ReviewRequiredError`, letting the calling application abort or handle it gracefully.
- **Non-Blocking Asynchronous Auditing**: Completed tool logs are queued in-memory and batch-shipped to the Governance Server asynchronously by a background worker thread.

---

## Installation

Install the package via `pip`:
```bash
pip install governance-sdk
```

---

## Quickstart

Initialize the SDK at the entry point of your agentic application.

### 1. SDK Initialization
`api_key` is the **only required parameter** to be passed to `init()`. All other settings (including the server URL) are pre-configured with secure autonomous defaults.

```python
import governance_sdk

# Only api_key is required
governance_sdk.init(
    api_key="your_api_key",
    project_name="my-agent-project"  # Optional
)
```

Alternatively, you can load the key from the environment variable:
```bash
export GOVERNANCE_API_KEY="your_api_key"
```
And initialize by passing the environment value:
```python
import os
import governance_sdk

governance_sdk.init(api_key=os.environ.get("GOVERNANCE_API_KEY"))
```

### 2. Auto-Interception & Context Capture
Once `init()` is called, any tool executed by your agent in LangChain, LangGraph, or CrewAI will be automatically intercepted:

```python
from langchain_core.tools import tool

@tool
def delete_user_data(user_id: str) -> str:
    """Deletes sensitive user information."""
    return f"User {user_id} deleted."

# In your agent logic:
# When this tool is called, the SDK automatically walks the stack to extract:
# - session_id (resolves from variables named session_id, sid, session, etc.)
# - agent_name (resolves from class name or variable agent_name)
# - purpose (resolves from variables named purpose, intent, reason, etc.)
# Then it verifies the risk score on the server side.
delete_user_data.run({"user_id": "usr_992"})
```

### 3. Explicit Context Override (Optional)
If you want to manually specify context instead of relying on the auto-captured stack frames, use the `agent_context` manager:

```python
import governance_sdk

with governance_sdk.agent_context(agent_name="Supervisor-Agent", session_id="session_override_123", purpose="cleanup"):
    # All tool calls executed inside this block will prioritize these values
    agent.run("delete temporary files")
```

---

## Captured Payload Format

The JSON payload sent to the governance logging server has the following structure:

```json
{
  "project_name": "customer-support-agent",
  "tool_calls": [
    {
      "tool_name": "delete_user_data",
      "tool_description": "Deletes sensitive user information.",
      "arguments": { "user_id": "usr_992" },
      "output": "User usr_992 deleted.",
      "error": null,
      "status": "success",
      "timestamp_start": "2026-07-30T13:30:00Z",
      "timestamp_end": "2026-07-30T13:30:00.045Z",
      "duration_ms": 45,
      "context": {
        "agent_name": "Supervisor-Agent",
        "session_id": "session_override_123",
        "purpose": "cleanup",
        "caller_filename": "main.py",
        "caller_line_number": 42,
        "caller_function": "execute_task"
      },
      "risk_score": 0.90,
      "risk_category": "high_risk",
      "governance_decision": "allow"
    }
  ]
}
```
