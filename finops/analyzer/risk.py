"""Risk tier assignment (§4.4).

* ``manual_only`` — tagged prod, untagged, or the action cannot be automated.
* ``safe_auto``   — reversible/no-downtime actions on non-prod: delete unattached EBS (after
  snapshot), gp2→gp3, stop an idle dev/test EC2 instance.
* ``approval``    — everything else (resizes need a restart).
"""

from __future__ import annotations

from finops.config import Rules
from finops.providers.aws import AUTOMATABLE_ACTIONS
from finops.providers.base import Resource

SAFE_AUTO = "safe_auto"
APPROVAL = "approval"
MANUAL = "manual_only"
TIERS = [SAFE_AUTO, APPROVAL, MANUAL]

_SAFE_ACTIONS = {"snapshot_and_delete_volume", "modify_volume"}


def is_prod(resource: Resource, rules: Rules) -> bool:
    value = resource.tags.get(rules.risk.prod_tag_key)
    if value is None:
        value = resource.env
    return (value or "").lower() in {v.lower() for v in rules.risk.prod_tag_values}


def risk_tier(resource: Resource, action_type: str | None, rules: Rules) -> str:
    if not resource.tags or is_prod(resource, rules):
        return MANUAL
    if action_type is None or action_type not in AUTOMATABLE_ACTIONS:
        return MANUAL
    if action_type in _SAFE_ACTIONS:
        return SAFE_AUTO
    if action_type == "stop_instance" and (resource.env or "") in rules.risk.non_prod_envs:
        return SAFE_AUTO
    return APPROVAL
