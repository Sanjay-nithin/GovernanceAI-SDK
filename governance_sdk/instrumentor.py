import functools
import inspect
import sys
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, Any, Dict

logger = logging.getLogger("governance_sdk")

_patched = {
    "langchain": False,
    "crewai": False
}

def get_caller_info() -> Dict[str, Any]:
    """Inspects the call stack to find the user script name and line number calling the tool."""
    stack = inspect.stack()
    caller_filename = "unknown"
    caller_line_number = 0
    caller_function = "unknown"
    
    for frame_info in stack:
        filename = frame_info.filename
        path_lower = filename.replace("\\", "/").lower()
        path_parts = path_lower.split("/")
        
        # Skip package internal files, decorators, and libraries
        if "governance_sdk" in path_parts or "importlib" in filename or "functools" in filename or "<string>" in filename:
            continue
            
        # Only skip frames belonging to the framework libraries themselves
        is_lib = any(p in path_parts for p in ["site-packages", "dist-packages", ".venv", "lib", "python" + str(sys.version_info.minor)])
        is_framework = any(fw in path_lower for fw in ["langchain", "crewai", "langgraph"])
        
        if is_lib and is_framework:
            continue
            
        caller_filename = os.path.basename(filename)
        caller_line_number = frame_info.lineno
        caller_function = frame_info.function
        break
        
    return {
        "caller_filename": caller_filename,
        "caller_line_number": caller_line_number,
        "caller_function": caller_function
    }

def _prepare_execution_context(client, tool_name: str, tool_desc: str, tool_input: Any):
    caller_info = get_caller_info()
    from governance_sdk.context import get_current_context
    context = get_current_context().copy()
    context.update(caller_info)
    
    start_time = time.time()
    start_iso = datetime.now(timezone.utc).isoformat()
    
    return caller_info, context, start_time, start_iso

def _evaluate_risk_and_permission(
    client,
    tool_name: str,
    tool_desc: str,
    tool_input: Any,
    context: dict,
    caller_info: dict,
    start_time: float,
    start_iso: str
):
    # 1. Pre-execution risk check
    risk_result = client.check_tool_risk(
        tool_name=tool_name,
        tool_description=tool_desc,
        arguments=tool_input,
        context=context
    )
    
    decision = risk_result.get("decision")
    reason = risk_result.get("reason", "Unknown reason")
    risk_category = risk_result.get("risk_category", "safe")
    risk_score = risk_result.get("risk_score", 0.0)
    
    was_authorized = False
    
    # 2. Interactive user permission prompting for preview_and_confirmation and needs_full_review
    if decision in ("preview_and_confirmation", "needs_full_review"):
        alert_type = "[Governance Alert]" if decision == "preview_and_confirmation" else "[Governance Review Required]"
        print(f"\n⚠️  {alert_type} Tool '{tool_name}' execution requested with '{risk_category}' risk level (Score: {risk_score}).", flush=True)
        print(f"   Reason: {reason}", flush=True)
        print(f"   Risky Parameters: {tool_input}", flush=True)
        print(f"   Triggered from: {caller_info['caller_filename']}:{caller_info['caller_line_number']} (in function '{caller_info['caller_function']}')", flush=True)
        
        try:
            user_input = input(f"👉  Do you want to authorize this execution? [y/N]: ").strip().lower()
        except Exception:
            user_input = "n"
            
        if user_input in ("y", "yes"):
            print("   [Governance] Authorized by user. Proceeding with execution...\n")
            decision = "allow"
            was_authorized = True
        else:
            print("   [Governance] Execution denied by user. Aborting...\n")
            
            # Send denied report to server
            end_time = time.time()
            end_iso = datetime.now(timezone.utc).isoformat()
            duration_ms = int((end_time - start_time) * 1000)
            
            client.send_tool_call({
                "tool_name": tool_name,
                "tool_description": tool_desc,
                "arguments": tool_input,
                "output": f"Blocked by Governance System. Reason: Permission denied by user. {reason}",
                "error": f"Execution Blocked: Permission denied by user. {reason}",
                "status": "blocked",
                "timestamp_start": start_iso,
                "timestamp_end": end_iso,
                "duration_ms": duration_ms,
                "context": context,
                "risk_score": risk_score,
                "risk_category": risk_category,
                "governance_decision": "denied"
            })
            
            from governance_sdk.exceptions import PermissionDeniedError, ReviewRequiredError
            if decision == "preview_and_confirmation":
                raise PermissionDeniedError(f"Permission denied by user. {reason}")
            else:
                raise ReviewRequiredError(f"Review required and denied by developer. {reason}")
                
    return decision, reason, risk_category, risk_score, was_authorized

def _report_execution_result(
    client,
    tool_name: str,
    tool_desc: str,
    tool_input: Any,
    context: dict,
    output: Any,
    error_msg: Optional[str],
    status: str,
    risk_score: float,
    risk_category: str,
    was_authorized: bool,
    start_time: float,
    start_iso: str
):
    end_time = time.time()
    end_iso = datetime.now(timezone.utc).isoformat()
    duration_ms = int((end_time - start_time) * 1000)
    
    client.send_tool_call({
        "tool_name": tool_name,
        "tool_description": tool_desc,
        "arguments": tool_input,
        "output": output,
        "error": error_msg,
        "status": status,
        "timestamp_start": start_iso,
        "timestamp_end": end_iso,
        "duration_ms": duration_ms,
        "context": context,
        "risk_score": risk_score,
        "risk_category": risk_category,
        "governance_decision": "authorized" if was_authorized else "allow"
    })

def patch_all(client) -> None:
    """Detects and patches LangChain and CrewAI tools."""
    patch_langchain(client)
    patch_crewai(client)

def patch_langchain(client) -> None:
    """Intercepts tool executions in LangChain."""
    if _patched["langchain"]:
        return
        
    try:
        from langchain_core.tools import BaseTool
        
        # Hook synchronous run
        original_run = BaseTool.run
        @functools.wraps(original_run)
        def wrapper_run(self, *args, **kwargs):
            tool_input = args[0] if len(args) > 0 else (kwargs.get("tool_input") or kwargs)
            tool_name = getattr(self, "name", "unknown_tool")
            tool_desc = getattr(self, "description", "")
            
            caller_info, context, start_time, start_iso = _prepare_execution_context(
                client, tool_name, tool_desc, tool_input
            )
            
            decision, reason, risk_category, risk_score, was_authorized = _evaluate_risk_and_permission(
                client, tool_name, tool_desc, tool_input, context, caller_info, start_time, start_iso
            )
            
            status = "success"
            output = None
            error_msg = None
            
            try:
                output = original_run(self, *args, **kwargs)
                return output
            except Exception as e:
                status = "failed"
                error_msg = f"{type(e).__name__}: {str(e)}"
                raise
            finally:
                _report_execution_result(
                    client, tool_name, tool_desc, tool_input, context, output, error_msg, status, risk_score, risk_category, was_authorized, start_time, start_iso
                )

        # Hook asynchronous run
        original_arun = BaseTool.arun
        @functools.wraps(original_arun)
        async def wrapper_arun(self, *args, **kwargs):
            tool_input = args[0] if len(args) > 0 else (kwargs.get("tool_input") or kwargs)
            tool_name = getattr(self, "name", "unknown_tool")
            tool_desc = getattr(self, "description", "")
            
            caller_info, context, start_time, start_iso = _prepare_execution_context(
                client, tool_name, tool_desc, tool_input
            )
            
            decision, reason, risk_category, risk_score, was_authorized = _evaluate_risk_and_permission(
                client, tool_name, tool_desc, tool_input, context, caller_info, start_time, start_iso
            )
            
            status = "success"
            output = None
            error_msg = None
            
            try:
                output = await original_arun(self, *args, **kwargs)
                return output
            except Exception as e:
                status = "failed"
                error_msg = f"{type(e).__name__}: {str(e)}"
                raise
            finally:
                _report_execution_result(
                    client, tool_name, tool_desc, tool_input, context, output, error_msg, status, risk_score, risk_category, was_authorized, start_time, start_iso
                )

        BaseTool.run = wrapper_run
        BaseTool.arun = wrapper_arun
        _patched["langchain"] = True
        logger.info("Successfully instrumented LangChain tools")
    except ImportError:
        pass


def patch_crewai(client) -> None:
    """Intercepts tool executions in CrewAI."""
    if _patched["crewai"]:
        return
        
    try:
        patched_any = False

        # 1. Patch BaseTool from crewai.tools
        try:
            from crewai.tools import BaseTool
            if hasattr(BaseTool, "run") and not getattr(BaseTool.run, "__wrapped__", None):
                original_run = BaseTool.run
                @functools.wraps(original_run)
                def wrapper_run(self, *args, **kwargs):
                    if len(args) == 1:
                        tool_input = args[0]
                    elif len(args) > 1:
                        tool_input = list(args)
                    else:
                        tool_input = kwargs.get("tool_input") or kwargs

                    tool_name = getattr(self, "name", "unknown_tool")
                    tool_desc = getattr(self, "description", "")
                    
                    caller_info, context, start_time, start_iso = _prepare_execution_context(
                        client, tool_name, tool_desc, tool_input
                    )
                    
                    decision, reason, risk_category, risk_score, was_authorized = _evaluate_risk_and_permission(
                        client, tool_name, tool_desc, tool_input, context, caller_info, start_time, start_iso
                    )
                    
                    status = "success"
                    output = None
                    error_msg = None
                    
                    try:
                        output = original_run(self, *args, **kwargs)
                        return output
                    except Exception as e:
                        status = "failed"
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        raise
                    finally:
                        _report_execution_result(
                            client, tool_name, tool_desc, tool_input, context, output, error_msg, status, risk_score, risk_category, was_authorized, start_time, start_iso
                        )

                original_arun = getattr(BaseTool, "arun", None)
                if original_arun and not getattr(original_arun, "__wrapped__", None):
                    @functools.wraps(original_arun)
                    async def wrapper_arun(self, *args, **kwargs):
                        if len(args) == 1:
                            tool_input = args[0]
                        elif len(args) > 1:
                            tool_input = list(args)
                        else:
                            tool_input = kwargs.get("tool_input") or kwargs

                        tool_name = getattr(self, "name", "unknown_tool")
                        tool_desc = getattr(self, "description", "")
                        
                        caller_info, context, start_time, start_iso = _prepare_execution_context(
                            client, tool_name, tool_desc, tool_input
                        )
                        
                        decision, reason, risk_category, risk_score, was_authorized = _evaluate_risk_and_permission(
                            client, tool_name, tool_desc, tool_input, context, caller_info, start_time, start_iso
                        )
                        
                        status = "success"
                        output = None
                        error_msg = None
                        
                        try:
                            output = await original_arun(self, *args, **kwargs)
                            return output
                        except Exception as e:
                            status = "failed"
                            error_msg = f"{type(e).__name__}: {str(e)}"
                            raise
                        finally:
                            _report_execution_result(
                                client, tool_name, tool_desc, tool_input, context, output, error_msg, status, risk_score, risk_category, was_authorized, start_time, start_iso
                            )
                    BaseTool.arun = wrapper_arun
                
                BaseTool.run = wrapper_run
                patched_any = True
        except ImportError:
            pass

        # 2. Patch Tool from crewai.tools.base_tool
        try:
            from crewai.tools.base_tool import Tool
            if hasattr(Tool, "run") and not getattr(Tool.run, "__wrapped__", None):
                original_run = Tool.run
                @functools.wraps(original_run)
                def wrapper_run(self, *args, **kwargs):
                    if len(args) == 1:
                        tool_input = args[0]
                    elif len(args) > 1:
                        tool_input = list(args)
                    else:
                        tool_input = kwargs.get("tool_input") or kwargs

                    tool_name = getattr(self, "name", "unknown_tool")
                    tool_desc = getattr(self, "description", "")
                    
                    caller_info, context, start_time, start_iso = _prepare_execution_context(
                        client, tool_name, tool_desc, tool_input
                    )
                    
                    decision, reason, risk_category, risk_score, was_authorized = _evaluate_risk_and_permission(
                        client, tool_name, tool_desc, tool_input, context, caller_info, start_time, start_iso
                    )
                    
                    status = "success"
                    output = None
                    error_msg = None
                    
                    try:
                        output = original_run(self, *args, **kwargs)
                        return output
                    except Exception as e:
                        status = "failed"
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        raise
                    finally:
                        _report_execution_result(
                            client, tool_name, tool_desc, tool_input, context, output, error_msg, status, risk_score, risk_category, was_authorized, start_time, start_iso
                        )

                original_arun = getattr(Tool, "arun", None)
                if original_arun and not getattr(original_arun, "__wrapped__", None):
                    @functools.wraps(original_arun)
                    async def wrapper_arun(self, *args, **kwargs):
                        if len(args) == 1:
                            tool_input = args[0]
                        elif len(args) > 1:
                            tool_input = list(args)
                        else:
                            tool_input = kwargs.get("tool_input") or kwargs

                        tool_name = getattr(self, "name", "unknown_tool")
                        tool_desc = getattr(self, "description", "")
                        
                        caller_info, context, start_time, start_iso = _prepare_execution_context(
                            client, tool_name, tool_desc, tool_input
                        )
                        
                        decision, reason, risk_category, risk_score, was_authorized = _evaluate_risk_and_permission(
                            client, tool_name, tool_desc, tool_input, context, caller_info, start_time, start_iso
                        )
                        
                        status = "success"
                        output = None
                        error_msg = None
                        
                        try:
                            output = await original_arun(self, *args, **kwargs)
                            return output
                        except Exception as e:
                            status = "failed"
                            error_msg = f"{type(e).__name__}: {str(e)}"
                            raise
                        finally:
                            _report_execution_result(
                                client, tool_name, tool_desc, tool_input, context, output, error_msg, status, risk_score, risk_category, was_authorized, start_time, start_iso
                            )
                    Tool.arun = wrapper_arun
                
                Tool.run = wrapper_run
                patched_any = True
        except ImportError:
            pass

        # 3. Patch CrewStructuredTool from crewai.tools.structured_tool
        try:
            from crewai.tools.structured_tool import CrewStructuredTool
            if hasattr(CrewStructuredTool, "invoke") and not getattr(CrewStructuredTool.invoke, "__wrapped__", None):
                original_invoke = CrewStructuredTool.invoke
                @functools.wraps(original_invoke)
                def wrapper_invoke(self, *args, **kwargs):
                    tool_input = args[0] if len(args) > 0 else kwargs.get("input", {})
                    # If input is a JSON string, try to parse it
                    if isinstance(tool_input, str):
                        try:
                            import json
                            tool_input = json.loads(tool_input)
                        except Exception:
                            pass
                    
                    tool_name = getattr(self, "name", "unknown_tool")
                    tool_desc = getattr(self, "description", "")
                    
                    caller_info, context, start_time, start_iso = _prepare_execution_context(
                        client, tool_name, tool_desc, tool_input
                    )
                    
                    decision, reason, risk_category, risk_score, was_authorized = _evaluate_risk_and_permission(
                        client, tool_name, tool_desc, tool_input, context, caller_info, start_time, start_iso
                    )
                    
                    status = "success"
                    output = None
                    error_msg = None
                    
                    try:
                        output = original_invoke(self, *args, **kwargs)
                        return output
                    except Exception as e:
                        status = "failed"
                        error_msg = f"{type(e).__name__}: {str(e)}"
                        raise
                    finally:
                        _report_execution_result(
                            client, tool_name, tool_desc, tool_input, context, output, error_msg, status, risk_score, risk_category, was_authorized, start_time, start_iso
                        )

                original_ainvoke = getattr(CrewStructuredTool, "ainvoke", None)
                if original_ainvoke and not getattr(original_ainvoke, "__wrapped__", None):
                    @functools.wraps(original_ainvoke)
                    async def wrapper_ainvoke(self, *args, **kwargs):
                        tool_input = args[0] if len(args) > 0 else kwargs.get("input", {})
                        if isinstance(tool_input, str):
                            try:
                                import json
                                tool_input = json.loads(tool_input)
                            except Exception:
                                pass
                        
                        tool_name = getattr(self, "name", "unknown_tool")
                        tool_desc = getattr(self, "description", "")
                        
                        caller_info, context, start_time, start_iso = _prepare_execution_context(
                            client, tool_name, tool_desc, tool_input
                        )
                        
                        decision, reason, risk_category, risk_score, was_authorized = _evaluate_risk_and_permission(
                            client, tool_name, tool_desc, tool_input, context, caller_info, start_time, start_iso
                        )
                        
                        status = "success"
                        output = None
                        error_msg = None
                        
                        try:
                            output = await original_ainvoke(self, *args, **kwargs)
                            return output
                        except Exception as e:
                            status = "failed"
                            error_msg = f"{type(e).__name__}: {str(e)}"
                            raise
                        finally:
                            _report_execution_result(
                                client, tool_name, tool_desc, tool_input, context, output, error_msg, status, risk_score, risk_category, was_authorized, start_time, start_iso
                            )
                    CrewStructuredTool.ainvoke = wrapper_ainvoke
                
                CrewStructuredTool.invoke = wrapper_invoke
                patched_any = True
        except ImportError:
            pass

        if patched_any:
            _patched["crewai"] = True
            logger.info("Successfully instrumented CrewAI tools")

    except Exception as e:
        logger.error(f"Error instrumenting CrewAI tools: {e}")

