from typing import Optional, Dict, Any

from governance_sdk.config import SDKConfig
from governance_sdk.client import GovernanceClient
from governance_sdk.context import agent_context
from governance_sdk.exceptions import GovernanceError, PermissionDeniedError, ReviewRequiredError
from governance_sdk.instrumentor import patch_all

_active_client: Optional[GovernanceClient] = None

def init(
    api_key: str,
    server_url: Optional[str] = None,
    risk_check_url: Optional[str] = None,
    project_name: Optional[str] = None,
    enabled: Optional[bool] = None,
    batch_size: Optional[int] = None,
    flush_interval: Optional[float] = None,
    max_queue_size: Optional[int] = None,
    fallback_policy: Optional[str] = None,
    risk_threshold: Optional[float] = None,
    extra_metadata: Optional[Dict[str, Any]] = None
) -> Optional[GovernanceClient]:
    """
    Initializes the Governance SDK.
    Starts the background worker queue and patches LangChain tool calling mechanisms.
    
    Example:
        import governance_sdk
        governance_sdk.init(
            server_url="https://governance.yourorg.com/api/v1/tool-calls",
            project_name="customer-support-agent"
        )
    """
    global _active_client
    
    if not api_key:
        raise ValueError("api_key is required to initialize the Governance SDK")
        
    config = SDKConfig(
        api_key=api_key,
        server_url=server_url,
        risk_check_url=risk_check_url,
        project_name=project_name,
        enabled=enabled,
        batch_size=batch_size,
        flush_interval=flush_interval,
        max_queue_size=max_queue_size,
        fallback_policy=fallback_policy,
        risk_threshold=risk_threshold,
        extra_metadata=extra_metadata
    )
    
    if not config.enabled: # is governance server enabled or not
        if _active_client:
            _active_client.shutdown()
            _active_client = None
        return None
        
    if not _active_client:
        _active_client = GovernanceClient(config)
        _active_client.start()
    else:
        _active_client.config = config
        _active_client.start()
        
    # Auto-patch LangChain tool calling
    patch_all(_active_client)
    
    return _active_client

def get_active_client() -> Optional[GovernanceClient]:
    """Retrieves the active global governance client."""
    return _active_client

__all__ = [
    "init",
    "agent_context",
    "get_active_client",
    "GovernanceError",
    "PermissionDeniedError",
    "ReviewRequiredError"
]

