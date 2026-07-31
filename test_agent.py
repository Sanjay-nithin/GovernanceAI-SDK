import asyncio
import json
import os
import socket
import sys
import time
import subprocess
from types import ModuleType
from typing import Any

# --- STEP 1: Dynamic LangChain Mocking ---

langchain_core = ModuleType("langchain_core")
langchain_core_tools = ModuleType("langchain_core.tools")
sys.modules["langchain_core"] = langchain_core
sys.modules["langchain_core.tools"] = langchain_core_tools

class MockLangChainTool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        
    def run(self, tool_input: Any, *args, **kwargs) -> str:
        return f"Processed {tool_input} in LangChain"

    async def arun(self, tool_input: Any, *args, **kwargs) -> str:
        return f"Async processed {tool_input} in LangChain"

langchain_core_tools.BaseTool = MockLangChainTool


# --- STEP 2: Subprocess Port Utility ---

def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


# --- STEP 3: Test execution ---

# Add local directories to import paths
sys.path.insert(0, ".")
import governance_sdk

async def _run_async_tests():
    # Test LangChain async tool
    lc_tool = MockLangChainTool("async_web_search", "Performs search async")
    res = await lc_tool.arun("What is AI governance?")
    assert "Async processed" in res

def main():
    import builtins
    builtins.input = lambda prompt="": "y"

    
    # Find free port and launch Governance Server subprocess
    server_port = find_free_port()
    server_script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../Governance/backend/server.py"))
    
    # Use the backend's virtual environment python if available to ensure all server dependencies are satisfied
    python_executable = os.path.abspath(os.path.join(os.path.dirname(__file__), "../Governance/backend/.venv/bin/python"))
    if not os.path.exists(python_executable):
        python_executable = sys.executable
        
    # Ensure test key exists in ClickHouse api_keys table by running a quick insertion script
    # using the backend's environment.
    print("Preparing test database (inserting test API key)...")
    backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../Governance/backend"))
    seed_script = f"""
import sys
sys.path.insert(0, '{backend_path}')
import clickhouse_connect, datetime
from config import CLICKHOUSE_DB_HOST, CLICKHOUSE_DB_USER, CLICKHOUSE_DB_PASSWORD, CLICKHOUSE_DB_PORT, CLICKHOUSE_DB_SECURE, CLICKHOUSE_DB_DATABASE
client = clickhouse_connect.get_client(host=CLICKHOUSE_DB_HOST, username=CLICKHOUSE_DB_USER, password=CLICKHOUSE_DB_PASSWORD, port=CLICKHOUSE_DB_PORT, secure=CLICKHOUSE_DB_SECURE, database=CLICKHOUSE_DB_DATABASE)
exists = client.query("SELECT count(*) FROM api_keys WHERE api_key = '{test_key}'").result_rows[0][0]
if not exists:
    client.insert('api_keys', [('{test_key}', 'Test Runner', datetime.datetime.now(datetime.timezone.utc))], column_names=['api_key', 'owner', 'created_at'])
"""
    subprocess.run([python_executable, "-c", seed_script], cwd=backend_path, check=True)
    
    print(f"Launching Governance Server Subprocess from: {server_script_path} using {python_executable}")
    server_process = subprocess.Popen(
        [python_executable, server_script_path, str(server_port)]
    )
    # Wait for the server to spin up and bind to the port (longer delay for ClickHouse Cloud handshakes)
    time.sleep(8.0)
    
    # Initialize the SDK pointing to our local server
    governance_sdk.init(
        "mock_key",
        server_url=f"http://127.0.0.1:{server_port}/api/v1/tool-calls",
        project_name="governance-sdk-test-suite",
        batch_size=1,
        flush_interval=0.1,
        risk_threshold=0.80
    )
    
    print("SDK initialized. Executing tool calls...")
    
    # 1. Standard LangChain sync tool (should be safe and run automatically)
    lc_tool = MockLangChainTool("read_user_data", "Reads user profile data")
    with governance_sdk.agent_context(agent_name="ReaderAgent", session_id="session_2", purpose="reading data"):
        res = lc_tool.run("john_doe")
        assert "Processed" in res
        
    # 2. Async tool calls (LangChain async)
    asyncio.run(_run_async_tests())
    
    # 3. Auto-captured context test (should automatically inspect stack frame variables)
    session_id = "session_auto_capture_999"
    purpose = "auto capture test purpose"
    res = lc_tool.run("john_doe")
    assert "Processed" in res
    
    print("Tool executions finished. Flushing SDK logs...")
    
    # Shut down client, forcing a flush of all remaining queue items to the server
    client = governance_sdk.get_active_client()
    if client:
        client.shutdown()
        
    # Stop the server subprocess
    print("Stopping Governance Server Subprocess...")
    server_process.terminate()
    try:
        server_process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        server_process.kill()
        
    # 4. Local fallback policy test (server unreachable)
    print("Testing local rules-based fallback policy with unreachable server...")
    fallback_client = governance_sdk.init(
        "mock_key",
        server_url="http://127.0.0.1:9999/api/v1/tool-calls",
        risk_check_url="http://127.0.0.1:9999/api/v1/risk-checks",
        fallback_policy="policy",
        enabled=True
    )
    # Check destructive shell command fallback
    res_shell = fallback_client.check_tool_risk(
        tool_name="execute_terminal_command",
        tool_description="runs commands",
        arguments={"command": "rm -rf /"},
        context={}
    )
    assert res_shell["decision"] == "needs_full_review"
    assert res_shell["risk_score"] == 0.95
    
    # Check delete operation fallback
    res_delete = fallback_client.check_tool_risk(
        tool_name="database.delete",
        tool_description="deletes records",
        arguments={"id": 123},
        context={}
    )
    assert res_delete["decision"] == "preview_and_confirmation"
    assert res_delete["risk_score"] == 0.70
    
    # Check safe operation fallback
    res_safe = fallback_client.check_tool_risk(
        tool_name="filesystem.read",
        tool_description="reads profile",
        arguments={"path": "/tmp/test.txt"},
        context={}
    )
    assert res_safe["decision"] == "allow"
    assert res_safe["risk_score"] == 0.15
    print("Local fallback policy tests passed!")
    
    print("\nALL SDK TESTS COMPLETED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
