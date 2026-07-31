import logging
import re
from typing import Dict, Any

logger = logging.getLogger("governance_sdk.fallback")

def _has_word(text: str, keywords: list) -> bool:
    """Helper to check if any keyword is present in the text as a complete word/boundary match."""
    for kw in keywords:
        if not kw.isalnum():
            # For non-alphanumeric keywords (like 'rm -rf'), do a simple substring match
            if kw in text:
                return True
        else:
            # Use word boundaries for alphanumeric keywords to avoid partial matches (e.g. 'rm' in 'performs')
            pattern = r'\b' + re.escape(kw) + r'\b'
            if re.search(pattern, text):
                return True
    return False

class LocalRiskEngine:
    """
    A lightweight, local risk-scoring engine used when the Governance Server is down.
    Analyzes tool metadata (name, description, arguments, context) using heuristics.
    """
    
    def __init__(self, risk_threshold: float = 0.70):
        self.risk_threshold = risk_threshold

    def calculate_risk(
        self,
        tool_name: str,
        tool_description: str,
        arguments: Any,
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Heuristically extracts risk features and calculates a deterministic risk score
        following the exact weights and overrides of the server-side scorer.
        """
        tool_lower = tool_name.lower().replace("_", " ").replace(".", " ")
        desc_lower = (tool_description or "").lower().replace("_", " ").replace(".", " ")
        args_str = str(arguments).lower()
        
        # 1. Feature Extraction: Asset Sensitivity
        # Determine asset classification
        sens = "LOW"
        asset_name = "unknown"
        
        # Extract possible asset names from arguments or name
        if "path" in args_str:
            asset_name = "filesystem"
        elif "db" in tool_lower or "database" in tool_lower:
            asset_name = "database"
        elif "command" in args_str or "exec" in tool_lower:
            asset_name = "system"
            
        # Determine Sensitivity
        if any(pat in args_str for pat in ["/etc/passwd", "/etc/shadow", ".ssh/", "private_key", "password"]):
            sens = "CRITICAL"
        elif _has_word(tool_lower, ["exec", "command", "shell"]):
            sens = "HIGH"
        elif _has_word(tool_lower, ["db", "database", "delete"]):
            sens = "HIGH"
        elif _has_word(tool_lower, ["write", "update"]):
            sens = "MEDIUM"
        else:
            sens = "LOW"
            
        asset_scores = {"LOW": 0.1, "MEDIUM": 0.3, "HIGH": 0.7, "CRITICAL": 1.0}
        asset_val = asset_scores.get(sens, 0.1)

        # 2. Feature Extraction: Impact Severity
        imp = "NEGLIGIBLE"
        action_type = "READ"
        data_loss_risk = False
        
        destructive_commands = ["rm -rf", "rm -f", "mkfs", "dd if=", "reboot", "shutdown", "chmod 777", "chown"]
        delete_keywords = ["delete", "remove", "destroy", "rm", "drop", "truncate"]
        
        is_shell = _has_word(tool_lower, ["shell", "terminal", "exec", "command", "system", "run"])
        has_destructive = any(cmd in args_str for cmd in destructive_commands)
        is_delete = _has_word(tool_lower, delete_keywords) or _has_word(desc_lower, delete_keywords)
        
        if is_shell and has_destructive:
            imp = "CATASTROPHIC"
            action_type = "EXECUTE"
            data_loss_risk = True
        elif is_delete:
            imp = "SEVERE"
            action_type = "DELETE"
            data_loss_risk = True
        elif _has_word(tool_lower, ["write", "update", "create"]):
            imp = "MODERATE"
            action_type = "WRITE"
        else:
            imp = "NEGLIGIBLE"
            action_type = "READ"
            
        impact_scores = {"NEGLIGIBLE": 0.1, "MODERATE": 0.3, "SEVERE": 0.7, "CATASTROPHIC": 1.0}
        impact_val = impact_scores.get(imp, 0.1)

        # 3. Feature Extraction: Context Risk
        s_risk = str(context.get("session_risk_factor", "LOW")).upper()
        context_scores = {"LOW": 0.1, "MEDIUM": 0.4, "HIGH": 0.8}
        context_val = context_scores.get(s_risk, 0.1)
        if not context.get("caller_verified", True):
            context_val = min(1.0, context_val + 0.2)

        # 4. Feature Extraction: Blast Radius / Scope
        scope = "LOCAL"
        if is_shell and has_destructive:
            scope = "GLOBAL"
        elif is_delete or "db" in tool_lower or "database" in tool_lower:
            scope = "PROJECT"
        else:
            scope = "LOCAL"
            
        scope_scores = {"LOCAL": 0.1, "PROJECT": 0.4, "GLOBAL": 0.9}
        scope_val = scope_scores.get(scope, 0.1)

        # 5. Calculate weighted score
        # (Asset * 30%) + (Impact * 30%) + (Context * 20%) + (Blast Radius * 20%)
        score = (asset_val * 0.3) + (impact_val * 0.3) + (context_val * 0.2) + (scope_val * 0.2)

        # 6. Apply deterministic overrides/rules to align with safety policy & tests
        if is_shell and has_destructive:
            score = max(score, 0.95)
        elif is_delete:
            score = max(score, 0.70)
        elif action_type == "READ" and sens == "LOW":
            score = 0.15
            
        score = round(score, 2)

        # 7. Determine decision based on score thresholds
        # Dynamic thresholds calculated from user-defined high risk threshold
        threshold = self.risk_threshold if self.risk_threshold is not None else 0.80
        high_threshold = threshold
        medium_threshold = round(high_threshold * 0.5, 2)
        
        if score >= high_threshold:
            decision = "needs_full_review"
            risk_category = "high_risk"
            reason_prefix = "Server unreachable. Local policy block: High risk operation."
        elif score >= medium_threshold:
            decision = "preview_and_confirmation"
            risk_category = "medium_risk"
            reason_prefix = "Server unreachable. Local policy warning: Threshold exceeded."
        else:
            decision = "allow"
            risk_category = "safe" if score <= round(medium_threshold * 0.5, 2) else "low_risk"
            reason_prefix = "Server unreachable. Local policy allow: Low risk operation."

        reason = (
            f"{reason_prefix} (Calculated local risk score {score} - {risk_category.replace('_', ' ').title()}). "
            f"Asset '{asset_name}' is classified as {sens} sensitivity. "
            f"Operation is {action_type} with {imp} impact. "
            f"Blast radius scope is {scope}."
        )

        return {
            "risk_score": score,
            "risk_category": risk_category,
            "decision": decision,
            "reason": reason
        }
