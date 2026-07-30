class GovernanceError(Exception):
    """Base exception for Governance SDK."""
    pass

class PermissionDeniedError(GovernanceError):
    """Raised when the user denies permission to execute a tool in the preview prompt."""
    pass

class ReviewRequiredError(GovernanceError):
    """Raised when a tool call requires a full review and is rejected by the developer."""
    pass
