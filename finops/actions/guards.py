"""Code-side safety checks. The same constraints are enforced in IAM (iam/remediator-policy.json)."""

from __future__ import annotations

from finops.config import Rules
from finops.providers.aws import AUTOMATABLE_ACTIONS
from finops.providers.base import Resource


def blocking_reasons(resource: Resource, action_type: str | None, risk_tier: str, rules: Rules) -> list[str]:
    """Return why ``action_type`` must not run against ``resource`` (empty list = allowed)."""
    risk = rules.risk
    reasons: list[str] = []
    if action_type is None or action_type not in AUTOMATABLE_ACTIONS:
        reasons.append(f"action {action_type!r} is manual-only")
    if resource.tags.get(risk.managed_tag_key, "").lower() != risk.managed_tag_value.lower():
        reasons.append(f"resource is not tagged {risk.managed_tag_key}={risk.managed_tag_value}")
    env = (resource.tags.get(risk.prod_tag_key) or resource.env or "").lower()
    if env in {v.lower() for v in risk.prod_tag_values}:
        reasons.append(f"resource is tagged {risk.prod_tag_key}={env}")
    if risk_tier == "manual_only":
        reasons.append("recommendation risk tier is manual_only")
    return reasons
