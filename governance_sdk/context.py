import contextlib
import contextvars
import inspect
import os
from typing import Dict, Any, Generator

# Define the thread-local / async-safe ContextVar
_context: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "governance_context", default={}
)

@contextlib.contextmanager
def agent_context(
    agent_name: str = None,
    session_id: str = None,
    trace_id: str = None,
    **kwargs
) -> Generator[None, None, None]:
    """
    Context manager to propagate metadata down the call stack.
    Useful for multi-agent workflows to tag which agent is calling which tool.
    
    Example:
        with agent_context(agent_name="Researcher", session_id="12345"):
            agent.run("find research papers")
    """
    # Get current context copy
    current = _context.get().copy()
    
    # Update with new values if provided
    if agent_name is not None:
        current["agent_name"] = agent_name
    if session_id is not None:
        current["session_id"] = session_id
    if trace_id is not None:
        current["trace_id"] = trace_id
        
    for k, v in kwargs.items():
        current[k] = v
        
    # Set the new context and store token
    token = _context.set(current)
    try:
        yield
    finally:
        # Restore previous context
        _context.reset(token)

def auto_capture_context() -> Dict[str, Any]:
    """Inspects the call stack to automatically capture agent execution context."""
    captured = {}
    try:
        stack = inspect.stack()
    except Exception:
        return captured
        
    for frame_info in stack:
        filename = frame_info.filename
        
        # Skip package internal files, decorators, importlib, and libraries
        if not filename or "governance_sdk" in filename or "importlib" in filename or "functools" in filename or "<string>" in filename:
            continue
        # Also skip langchain or other common library frameworks to avoid looking at library internals
        if "langchain_core" in filename or "langchain_community" in filename or ("langchain/" in filename.replace("\\", "/")) or "crewai" in filename or "site-packages" in filename:
            continue
            
        frame = frame_info.frame
        locals_dict = frame.f_locals
        
        # 1. Search for session_id
        if "session_id" not in captured:
            for key in ["session_id", "sessionId", "session", "session_uuid", "sid"]:
                if key in locals_dict:
                    val = locals_dict[key]
                    if isinstance(val, str) and val.strip():
                        captured["session_id"] = val.strip()
                        break
                    elif hasattr(val, "session_id") and isinstance(getattr(val, "session_id"), str):
                        captured["session_id"] = getattr(val, "session_id").strip()
                        break
                    elif hasattr(val, "id") and isinstance(getattr(val, "id"), str):
                        captured["session_id"] = getattr(val, "id").strip()
                        break
                        
        # 2. Search for agent_name
        if "agent_name" not in captured:
            for key in ["agent_name", "agentName", "agent_id"]:
                if key in locals_dict:
                    val = locals_dict[key]
                    if isinstance(val, str) and val.strip():
                        captured["agent_name"] = val.strip()
                        break
            
            # If not found, check self
            if "agent_name" not in captured and "self" in locals_dict:
                self_obj = locals_dict["self"]
                if hasattr(self_obj, "name") and isinstance(getattr(self_obj, "name"), str) and getattr(self_obj, "name").strip():
                    captured["agent_name"] = getattr(self_obj, "name").strip()
                elif hasattr(self_obj, "__class__"):
                    class_name = self_obj.__class__.__name__
                    if class_name not in ("dict", "list", "str", "int", "float", "bool", "tuple", "set", "object", "module", "function", "builtin_function_or_method", "type"):
                        captured["agent_name"] = class_name
            
            # Check if any local variable is an agent object
            if "agent_name" not in captured:
                for k, val in locals_dict.items():
                    if k == "agent" or k.endswith("_agent") or "agent" in k.lower():
                        if hasattr(val, "__class__"):
                            class_name = val.__class__.__name__
                            if class_name not in ("dict", "list", "str", "int", "float", "bool", "tuple", "set", "object", "module", "function", "builtin_function_or_method", "type"):
                                captured["agent_name"] = class_name
                                break
                            elif isinstance(val, str) and val.strip():
                                captured["agent_name"] = val.strip()
                                break
                                
        # 3. Search for purpose
        if "purpose" not in captured:
            for key in ["purpose", "intent", "reason", "goal", "step_purpose"]:
                if key in locals_dict:
                    val = locals_dict[key]
                    if isinstance(val, str) and val.strip():
                        captured["purpose"] = val.strip()
                        break
            
            # Check step or task dictionary/object
            if "purpose" not in captured:
                for key in ["step", "task", "tool_call"]:
                    if key in locals_dict:
                        val = locals_dict[key]
                        if isinstance(val, dict) and "purpose" in val and isinstance(val["purpose"], str) and val["purpose"].strip():
                            captured["purpose"] = val["purpose"].strip()
                            break
                        elif hasattr(val, "purpose") and isinstance(getattr(val, "purpose"), str) and getattr(val, "purpose").strip():
                            captured["purpose"] = getattr(val, "purpose").strip()
                            break
                            
        # 4. Search for trace_id
        if "trace_id" not in captured:
            for key in ["trace_id", "traceId", "correlation_id", "correlationId"]:
                if key in locals_dict:
                    val = locals_dict[key]
                    if isinstance(val, str) and val.strip():
                        captured["trace_id"] = val.strip()
                        break

    # If agent_name is still not found, we can use the caller function name
    if "agent_name" not in captured:
        for frame_info in stack:
            filename = frame_info.filename
            if not filename or "governance_sdk" in filename or "importlib" in filename or "functools" in filename or "<string>" in filename:
                continue
            if "langchain" in filename:
                continue
            func_name = frame_info.function
            if func_name not in ("wrapper_run", "wrapper_arun", "run", "arun", "<module>"):
                if func_name == "execute_plan":
                    captured["agent_name"] = "ExecutorAgent"
                else:
                    captured["agent_name"] = func_name
                break
        if "agent_name" not in captured:
            captured["agent_name"] = "UnknownAgent"
            
    return captured

def get_current_context() -> Dict[str, Any]:
    """Returns a copy of the current context variables, supplemented with auto-captured values."""
    ctx = _context.get().copy()
    
    auto_ctx = auto_capture_context()
    for k, v in auto_ctx.items():
        if k not in ctx or ctx[k] is None:
            ctx[k] = v
            
    return ctx

