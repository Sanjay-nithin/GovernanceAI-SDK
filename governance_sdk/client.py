import atexit
import json
import logging
import queue
import threading
import time
from typing import Dict, Any, List, Optional
import urllib.request
import urllib.error

from governance_sdk.config import SDKConfig

logger = logging.getLogger("governance_sdk")

def safe_serialize(obj: Any) -> Any:
    """Safely serializes objects that might not be JSON serializable."""
    try:
        json.dumps(obj)
        return obj
    except (TypeError, OverflowError):
        if isinstance(obj, dict):
            return {str(k): safe_serialize(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple, set)):
            return [safe_serialize(x) for x in obj]
        else:
            try:
                if hasattr(obj, "__dict__"):
                    return safe_serialize(obj.__dict__)
                return repr(obj)
            except Exception:
                return str(obj)

class GovernanceClient:
    def __init__(self, config: SDKConfig):
        self.config = config
        self.queue: queue.Queue = queue.Queue(maxsize=config.max_queue_size)
        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        
    def start(self) -> None:
        """Starts the background worker thread."""
        with self.lock:
            if self.worker_thread and self.worker_thread.is_alive():
                return
            self.stop_event.clear()
            self.worker_thread = threading.Thread(
                target=self._worker_loop, 
                name="governance-sdk-worker", 
                daemon=True
            )
            self.worker_thread.start()
            atexit.register(self.shutdown)

    def send_tool_call(self, tool_call_data: Dict[str, Any]) -> None:
        """Enqueues a tool call payload without blocking."""
        if not self.config.is_configured():
            return
            
        if not self.worker_thread or not self.worker_thread.is_alive():
            self.start()

        sanitized = safe_serialize(tool_call_data)
        
        try:
            self.queue.put_nowait(sanitized)
        except queue.Full:
            logger.warning("Governance SDK queue is full. Dropping tool call report.")

    def check_tool_risk(
        self,
        tool_name: str,
        tool_description: str,
        arguments: Any,
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Synchronously queries the governance server to evaluate the risk score of a tool call before execution.
        Categorizes risk and returns decision (allow, preview_and_confirmation, needs_full_review).
        """
        if not self.config.is_configured():
            return {
                "decision": "allow", 
                "risk_score": 0.0, 
                "risk_category": "safe", 
                "reason": "Governance SDK is disabled or not configured"
            }
            
        payload = {
            "project_name": self.config.project_name,
            "tool_name": tool_name,
            "tool_description": tool_description,
            "arguments": safe_serialize(arguments),
            "context": safe_serialize(context),
            "risk_threshold": self.config.risk_threshold
        }
        
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "governance-sdk-python/0.1.0"
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
            
        try:
            req = urllib.request.Request(
                self.config.risk_check_url,
                data=data,
                headers=headers,
                method="POST"
            )
            # Use a 10-second timeout for risk assessment to support multi-agent analysis
            with urllib.request.urlopen(req, timeout=10.0) as response:
                if 200 <= response.status < 300:
                    resp_data = json.loads(response.read().decode("utf-8"))
                    
                    risk_score = float(resp_data.get("risk_score", 0.0))
                    raw_decision = resp_data.get("decision", "allow").lower()
                    
                    # Categorize risk score if not provided by server
                    if "risk_category" in resp_data:
                        risk_category = resp_data["risk_category"].lower()
                    else:
                        if risk_score >= 0.80:
                            risk_category = "high_risk"
                        elif risk_score >= 0.60:
                            risk_category = "medium_risk"
                        elif risk_score >= 0.30:
                            risk_category = "low_risk"
                        else:
                            risk_category = "safe"
                            
                    reason = resp_data.get("reason", "Approved by default policy")
                    
                    # Local risk override guardrail: map high risk or threshold exceedance to user prompt
                    if raw_decision == "allow" and risk_category == "high_risk":
                        decision = "preview_and_confirmation"
                        reason = f"Local override: High risk score ({risk_score}) requires permission. {reason}"
                    elif raw_decision == "allow" and risk_score > self.config.risk_threshold:
                        decision = "preview_and_confirmation"
                        reason = f"Local override: Threshold exceeded ({risk_score} > {self.config.risk_threshold}) requires permission. {reason}"
                    else:
                        # Standardize decision to the three allowed options
                        if raw_decision in ("preview_and_confirmation", "needs_permission"):
                            decision = "preview_and_confirmation"
                        elif raw_decision in ("needs_full_review", "block"):
                            decision = "needs_full_review"
                        else:
                            decision = "allow"
                        
                    return {
                        "decision": decision,
                        "risk_score": risk_score,
                        "risk_category": risk_category,
                        "reason": reason
                    }
        except Exception as e:
            logger.warning(f"Governance Server risk-check failed: {e}. Falling back to '{self.config.fallback_policy}' policy.")
            
        # Fallback handling
        fallback_decision = self.config.fallback_policy
        if fallback_decision == "policy":
            return self._evaluate_local_fallback_policy(tool_name, tool_description, arguments, context)
        elif fallback_decision == "block":
            return {
                "decision": "needs_full_review",
                "risk_score": 1.0,
                "risk_category": "high_risk",
                "reason": "Server unreachable. Applied fallback closed policy: block."
            }
        else:
            return {
                "decision": "allow",
                "risk_score": 0.0,
                "risk_category": "safe",
                "reason": "Server unreachable. Applied fallback open policy: allow."
            }

    def _evaluate_local_fallback_policy(
        self,
        tool_name: str,
        tool_description: str,
        arguments: Any,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Evaluates a local rules-based safety policy using the LocalRiskEngine when the Governance Server is unreachable.
        """
        try:
            from governance_sdk.fallback import LocalRiskEngine
            engine = LocalRiskEngine(risk_threshold=self.config.risk_threshold)
            return engine.calculate_risk(tool_name, tool_description, arguments, context or {})
        except Exception as e:
            logger.error(f"Failed to run local fallback risk engine: {e}")
            return {
                "decision": "needs_full_review" if self.config.fallback_policy == "block" else "allow",
                "risk_score": 0.5,
                "risk_category": "medium_risk",
                "reason": f"Local fallback engine exception: {e}"
            }


    def _worker_loop(self) -> None:
        """Background thread loop that batches and sends tool logs."""
        batch_size = self.config.batch_size
        flush_interval = self.config.flush_interval
        
        while not self.stop_event.is_set():
            batch = self._gather_batch(batch_size, timeout=flush_interval)
            if batch:
                self._send_batch_with_retry(batch)
                
        self._flush_remaining()

    def _gather_batch(self, batch_size: int, timeout: float) -> List[Dict[str, Any]]:
        batch = []
        start_time = time.time()
        
        while len(batch) < batch_size:
            remaining_time = timeout - (time.time() - start_time)
            if remaining_time <= 0:
                break
                
            try:
                item = self.queue.get(timeout=max(0.01, remaining_time))
                batch.append(item)
                self.queue.task_done()
            except queue.Empty:
                break
                
        return batch

    def _send_batch_with_retry(self, batch: List[Dict[str, Any]]) -> None:
        payload = {
            "project_name": self.config.project_name,
            "tool_calls": batch
        }
        
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "governance-sdk-python/0.1.0"
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        backoff = 1.0
        max_backoff = 30.0
        retries = 3
        
        for attempt in range(retries):
            if self.stop_event.is_set() and attempt > 0:
                break
                
            try:
                req = urllib.request.Request(
                    self.config.server_url,
                    data=data,
                    headers=headers,
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=5.0) as response:
                    if 200 <= response.status < 300:
                        return
            except (urllib.error.URLError, Exception) as e:
                if attempt < retries - 1:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
                else:
                    logger.error(f"Governance SDK failed to send {len(batch)} tool logs: {e}")

    def _flush_remaining(self) -> None:
        while not self.queue.empty():
            batch = []
            while len(batch) < self.config.batch_size:
                try:
                    item = self.queue.get_nowait()
                    batch.append(item)
                    self.queue.task_done()
                except queue.Empty:
                    break
            if batch:
                self._send_batch_with_retry(batch)

    def shutdown(self) -> None:
        with self.lock:
            if self.stop_event.is_set():
                return
            self.stop_event.set()
            
        if self.worker_thread:
            self.worker_thread.join(timeout=3.0)
            self.worker_thread = None
            
        try:
            atexit.unregister(self.shutdown)
        except Exception:
            pass
