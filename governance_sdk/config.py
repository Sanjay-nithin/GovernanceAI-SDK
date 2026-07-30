import os
from typing import Optional, Dict, Any

class SDKConfig:
    def __init__(
        self,
        server_url: Optional[str] = None,
        risk_check_url: Optional[str] = None,
        api_key: Optional[str] = None,
        project_name: Optional[str] = None,
        enabled: Optional[bool] = None,
        batch_size: Optional[int] = None,
        flush_interval: Optional[float] = None,
        max_queue_size: Optional[int] = None,
        fallback_policy: Optional[str] = None,
        risk_threshold: Optional[float] = None,
        extra_metadata: Optional[Dict[str, Any]] = None
    ):
        # Base and risk check URLs
        self.server_url = server_url or os.environ.get("GOVERNANCE_SERVER_URL", "http://127.0.0.1:8000/api/v1/tool-calls")
        self.risk_check_url = risk_check_url or os.environ.get("GOVERNANCE_RISK_CHECK_URL") or self.server_url.replace("tool-calls", "risk-checks")
        
        self.api_key = api_key or os.environ.get("GOVERNANCE_API_KEY")
        self.project_name = project_name or os.environ.get("GOVERNANCE_PROJECT_NAME", "default-project")
        
        env_enabled = os.environ.get("GOVERNANCE_ENABLED", "true").lower() in ("true", "1", "yes")
        # An API key must be produced to enable the SDK and intercept tool calls
        self.enabled = (enabled if enabled is not None else env_enabled) and bool(self.api_key)
        
        # Batching configuration
        self.batch_size = batch_size or int(os.environ.get("GOVERNANCE_BATCH_SIZE", "10"))
        self.flush_interval = flush_interval or float(os.environ.get("GOVERNANCE_FLUSH_INTERVAL", "1.0"))
        self.max_queue_size = max_queue_size or int(os.environ.get("GOVERNANCE_MAX_QUEUE_SIZE", "1000"))
        
        # Fallback policy when governance server is down: "allow", "block", or "policy" (local rules evaluation)
        self.fallback_policy = fallback_policy or os.environ.get("GOVERNANCE_FALLBACK_POLICY", "policy").lower()
        if self.fallback_policy not in ("allow", "block", "policy"):
            self.fallback_policy = "policy"
            
        # Default risk threshold above which we block execution
        env_threshold = os.environ.get("GOVERNANCE_RISK_THRESHOLD")
        self.risk_threshold = risk_threshold if risk_threshold is not None else (float(env_threshold) if env_threshold else 0.70)
        
        self.extra_metadata = extra_metadata or {}

    def is_configured(self) -> bool:
        """Returns True if the SDK is enabled, has a valid server URL, and has a valid API key."""
        return self.enabled and bool(self.server_url) and bool(self.api_key)
