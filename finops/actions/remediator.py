"""Remediator (§4.6): dry-run → approve → execute, with a full audit trail in ``actions``.

Execution requires *all* of:
  * guards pass (``finops:managed=true``, not ``env=prod``, automatable, tier ≠ manual_only);
  * the action is ``approved`` — or it is ``safe_auto`` and ``FINOPS_AUTO_APPLY=true``;
  * the resource still matches the spec the recommendation was made against (no stale actions);
  * ``dry_run=False`` was passed explicitly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import boto3
from sqlalchemy import select

from finops.analyzer.engine import row_to_resource
from finops.config import Rules, Settings, get_rules, get_settings
from finops.db import ActionRow, RecommendationRow, Run, session_scope
from finops.providers.aws import AwsProvider
from finops.providers.base import Action, CloudProvider

from .guards import blocking_reasons

log = logging.getLogger(__name__)

OPEN_STATUSES = ("dry_run", "approved")
SPEC_KEYS = {"ec2": ("instance_type",), "ebs": ("volume_type", "size_gb"), "rds": ("instance_class", "multi_az")}


class RemediationError(Exception):
    pass


def build_action(rec: RecommendationRow) -> Action:
    res, spec = rec.resource, rec.recommended_spec or {}
    params: dict[str, Any] = {}
    if rec.action_type == "modify_instance_type":
        params["target_type"] = spec["instance_type"]
    elif rec.action_type == "modify_volume":
        params = {k: spec.get(k) for k in ("volume_type", "iops", "throughput")}
    elif rec.action_type == "modify_db_instance_class":
        params["target_class"] = spec["instance_class"]
    return Action(rec.action_type or "", res.id, res.type, res.region, params)


def _dry_run_provider(region: str) -> AwsProvider:
    # describe_action needs no credentials; avoid requiring the remediator profile for dry-runs.
    return AwsProvider(region, session=boto3.Session(region_name=region))


class Remediator:
    def __init__(
        self,
        provider_factory: Callable[[str], CloudProvider] | None = None,
        settings: Settings | None = None,
        rules: Rules | None = None,
    ):
        self.settings = settings or get_settings()
        self.rules = rules or get_rules()
        self.provider_factory = provider_factory or (
            lambda region: AwsProvider(region, profile=self.settings.aws_profile_remediator)
        )

    # ------------------------------------------------------------------ helpers
    def _guard(self, rec: RecommendationRow) -> list[str]:
        return blocking_reasons(row_to_resource(rec.resource), rec.action_type, rec.risk_tier, self.rules)

    @staticmethod
    def _open_action(session, rec: RecommendationRow) -> ActionRow | None:
        return session.scalar(
            select(ActionRow)
            .where(ActionRow.recommendation_id == rec.id, ActionRow.status.in_(OPEN_STATUSES))
            .order_by(ActionRow.id.desc())
        )

    @staticmethod
    def _append_log(action: ActionRow, message: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        action.log = (action.log + "\n" if action.log else "") + f"[{stamp}] {message}"

    # ------------------------------------------------------------------ API
    def plan(self, recommendation_id: int, requested_by: str = "system") -> dict[str, Any]:
        """Create (or refresh) a dry-run action showing the exact API calls."""
        with session_scope() as s:
            rec = s.get(RecommendationRow, recommendation_id)
            if rec is None:
                raise RemediationError(f"recommendation {recommendation_id} not found")
            if not rec.action_type:
                raise RemediationError("recommendation has no action (right_sized / insufficient_data)")
            action = build_action(rec)
            try:
                plan = _dry_run_provider(action.region).describe_action(action)
            except ValueError as exc:
                plan = [{"manual": str(exc)}]
            row = self._open_action(s, rec)
            if row is None:
                row = ActionRow(recommendation_id=rec.id, action_type=rec.action_type, requested_by=requested_by)
                s.add(row)
            row.plan = plan
            reasons = self._guard(rec)
            self._append_log(row, "dry-run plan: " + json.dumps(plan, default=str))
            if reasons:
                self._append_log(row, "would be BLOCKED: " + "; ".join(reasons))
            s.flush()
            return self._to_dict(row, blocked_reasons=reasons)

    def approve(self, recommendation_id: int, approved_by: str) -> dict[str, Any]:
        self.plan(recommendation_id, requested_by=approved_by)
        with session_scope() as s:
            rec = s.get(RecommendationRow, recommendation_id)
            row = self._open_action(s, rec)
            reasons = self._guard(rec)
            if reasons:
                row.status = "blocked"
                self._append_log(row, f"approval by {approved_by} refused: " + "; ".join(reasons))
                s.flush()
                return self._to_dict(row, blocked_reasons=reasons)
            row.status, row.approved_by = "approved", approved_by
            self._append_log(row, f"approved by {approved_by}")
            s.flush()
            return self._to_dict(row)

    def execute(self, action_id: int, dry_run: bool = True, actor: str = "system") -> dict[str, Any]:
        with session_scope() as s:
            row = s.get(ActionRow, action_id)
            if row is None:
                raise RemediationError(f"action {action_id} not found")
            rec = row.recommendation
            reasons = self._guard(rec)
            if dry_run:
                self._append_log(row, f"dry-run requested by {actor}; no changes made")
                return self._to_dict(row, blocked_reasons=reasons, dry_run=True)
            if row.status not in OPEN_STATUSES:
                raise RemediationError(f"action {action_id} is {row.status}; cannot execute")
            auto_ok = rec.risk_tier == "safe_auto" and self.settings.auto_apply
            if row.status != "approved" and not auto_ok:
                raise RemediationError("action is not approved (and is not an auto-applicable safe_auto action)")
            if reasons:
                row.status = "blocked"
                self._append_log(row, "execution BLOCKED: " + "; ".join(reasons))
                return self._to_dict(row, blocked_reasons=reasons)

            action = build_action(rec)
            provider = self.provider_factory(action.region)
            try:
                stale = self._stale(provider, action, rec)
                if stale:
                    row.status = "blocked"
                    self._append_log(row, f"execution BLOCKED: resource changed since analysis ({stale})")
                    return self._to_dict(row, blocked_reasons=[stale])
                self._append_log(row, f"executing as {actor}")
                result = provider.apply_action(action)
            except Exception as exc:
                row.status = "failed"
                row.executed_at = datetime.now(UTC)
                self._append_log(row, f"FAILED: {exc}")
                log.exception("action %s failed", action_id)
                return self._to_dict(row)
            row.status = "executed"
            row.executed_at = datetime.now(UTC)
            row.before_state, row.after_state = result.get("before", {}), result.get("after", {})
            if result.get("snapshot_id"):
                row.after_state = {**row.after_state, "snapshot_id": result["snapshot_id"]}
            row.rollback = result.get("rollback", "")
            for line in result.get("log", []):
                self._append_log(row, line)
            return self._to_dict(row)

    def mark_rolled_back(self, action_id: int, actor: str, note: str = "") -> dict[str, Any]:
        with session_scope() as s:
            row = s.get(ActionRow, action_id)
            if row is None or row.status != "executed":
                raise RemediationError("only executed actions can be marked rolled back")
            row.status = "rolled_back"
            self._append_log(row, f"rolled back by {actor}. {note}".strip())
            return self._to_dict(row)

    def auto_apply(self, run_id: int | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        """Execute every ``safe_auto`` recommendation of a run (only when FINOPS_AUTO_APPLY=true)."""
        if not self.settings.auto_apply and not dry_run:
            return []
        with session_scope() as s:
            if run_id is None:
                run_id = s.scalar(select(Run.id).where(Run.kind == "analyze").order_by(Run.id.desc()))
            ids = s.scalars(
                select(RecommendationRow.id).where(
                    RecommendationRow.run_id == run_id, RecommendationRow.risk_tier == "safe_auto"
                )
            ).all()
        results = []
        for rec_id in ids:
            planned = self.plan(rec_id, requested_by="auto_apply")
            if planned.get("blocked_reasons"):
                results.append(planned)
                continue
            results.append(self.execute(planned["id"], dry_run=dry_run, actor="auto_apply"))
        return results

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _stale(provider: CloudProvider, action: Action, rec: RecommendationRow) -> str | None:
        snapshot = getattr(provider, "_snapshot_state", None)
        if snapshot is None:
            return None
        live = snapshot(action)
        if live.get("state") == "deleted":
            return "resource no longer exists"
        for key in SPEC_KEYS.get(action.resource_type, ()):
            if key in live and key in rec.current_spec and live[key] != rec.current_spec[key]:
                return f"{key} is {live[key]!r}, analysis assumed {rec.current_spec[key]!r}"
        return None

    @staticmethod
    def _to_dict(row: ActionRow, **extra: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "recommendation_id": row.recommendation_id,
            "action_type": row.action_type,
            "status": row.status,
            "requested_by": row.requested_by,
            "approved_by": row.approved_by,
            "executed_at": row.executed_at.isoformat() if row.executed_at else None,
            "plan": row.plan,
            "before_state": row.before_state,
            "after_state": row.after_state,
            "rollback": row.rollback,
            "log": row.log,
            **extra,
        }
