"""Read/query layer shared by the API and the CLI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from finops.analyzer.engine import run_analysis
from finops.collector import Collector, providers_from_settings
from finops.config import get_settings
from finops.db import (
    ActionRow,
    BillingSnapshot,
    MetricHourly,
    RecommendationRow,
    ResourceRow,
    Run,
    session_scope,
)
from finops.savings.engine import aggregate, to_inr


def latest_run_id() -> int | None:
    with session_scope() as s:
        return s.scalar(select(Run.id).where(Run.kind == "analyze", Run.status == "ok").order_by(Run.id.desc()))


def rec_to_dict(r: RecommendationRow) -> dict[str, Any]:
    rate = get_settings().usd_to_inr
    return {
        "id": r.id,
        "run_id": r.run_id,
        "resource_id": r.resource_id,
        "resource_type": r.resource.type,
        "resource_name": r.resource.name,
        "region": r.resource.region,
        "tags": r.resource.tags,
        "classification": r.classification,
        "confidence": r.confidence,
        "risk_tier": r.risk_tier,
        "action_type": r.action_type,
        "current_spec": r.current_spec,
        "recommended_spec": r.recommended_spec,
        "current_monthly_cost": round(r.current_monthly_cost, 2),
        "recommended_monthly_cost": round(r.recommended_monthly_cost, 2),
        "monthly_savings": round(r.monthly_savings, 2),
        "monthly_savings_inr": to_inr(r.monthly_savings, rate),
        "rationale": r.rationale,
        "created_at": r.created_at.isoformat(),
    }


def recommendations(
    run_id: int | None = None,
    risk_tier: str | None = None,
    classification: str | None = None,
    resource_type: str | None = None,
) -> list[dict[str, Any]]:
    run_id = run_id or latest_run_id()
    if run_id is None:
        return []
    with session_scope() as s:
        q = select(RecommendationRow).where(RecommendationRow.run_id == run_id)
        if risk_tier:
            q = q.where(RecommendationRow.risk_tier == risk_tier)
        if classification:
            q = q.where(RecommendationRow.classification == classification)
        rows = s.scalars(q.order_by(RecommendationRow.monthly_savings.desc())).all()
        out = [rec_to_dict(r) for r in rows]
    if resource_type:
        out = [r for r in out if r["resource_type"] == resource_type]
    return out


def resources(resource_type: str | None = None, classification: str | None = None) -> list[dict[str, Any]]:
    recs = {r["resource_id"]: r for r in recommendations()}
    with session_scope() as s:
        q = select(ResourceRow)
        if resource_type:
            q = q.where(ResourceRow.type == resource_type)
        rows = s.scalars(q).all()
        out = []
        for row in rows:
            rec = recs.get(row.id)
            if classification and (rec is None or rec["classification"] != classification):
                continue
            out.append({
                "id": row.id, "type": row.type, "name": row.name, "region": row.region, "state": row.state,
                "tags": row.tags, "spec": row.spec, "last_seen": row.last_seen.isoformat(),
                "classification": rec["classification"] if rec else None,
                "risk_tier": rec["risk_tier"] if rec else None,
                "recommendation_id": rec["id"] if rec else None,
                "recommended_spec": rec["recommended_spec"] if rec else None,
                "current_monthly_cost": rec["current_monthly_cost"] if rec else None,
                "monthly_savings": rec["monthly_savings"] if rec else None,
                "p95_cpu": (rec["rationale"] or {}).get("p95_cpu") if rec else None,
            })  # fmt: skip
    return out


def metric_series(resource_id: str, metric: str, days: int = 30) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=days)
    with session_scope() as s:
        rows = s.scalars(
            select(MetricHourly)
            .where(MetricHourly.resource_id == resource_id, MetricHourly.metric == metric, MetricHourly.ts >= since)
            .order_by(MetricHourly.ts)
        ).all()
        return [{"ts": m.ts.isoformat(), "p50": m.p50, "p95": m.p95, "max": m.max} for m in rows]


def summary() -> dict[str, Any]:
    settings = get_settings()
    run_id = latest_run_id()
    with session_scope() as s:
        recs = s.scalars(select(RecommendationRow).where(RecommendationRow.run_id == run_id)).all() if run_id else []
        agg = aggregate(recs)
        bill = s.scalar(
            select(BillingSnapshot)
            .where(BillingSnapshot.scope == "account")
            .order_by(BillingSnapshot.fetched_at.desc())
        )
        billing = (
            {"by_service": bill.by_service, "total": bill.total, "period_start": bill.period_start.isoformat(),
             "period_end": bill.period_end.isoformat(), "fetched_at": bill.fetched_at.isoformat()}
            if bill else None
        )  # fmt: skip
        run = s.get(Run, run_id) if run_id else None
        last_run = (
            {"id": run.id, "finished_at": run.finished_at.isoformat() if run.finished_at else None} if run else None
        )
    return {
        **agg,
        "run": last_run,
        "billing": billing,
        "usd_to_inr": settings.usd_to_inr,
        "total_monthly_savings_inr": to_inr(agg["total_monthly_savings"], settings.usd_to_inr),
        "total_current_monthly_inr": to_inr(agg["total_current_monthly"], settings.usd_to_inr),
    }


def actions(status: str | None = None) -> list[dict[str, Any]]:
    from finops.actions.remediator import Remediator

    with session_scope() as s:
        q = select(ActionRow).order_by(ActionRow.id.desc())
        if status:
            q = q.where(ActionRow.status == status)
        out = []
        for a in s.scalars(q):
            d = Remediator._to_dict(a)
            d["resource_id"] = a.recommendation.resource_id
            d["risk_tier"] = a.recommendation.risk_tier
            out.append(d)
        return out


def runs(limit: int = 50) -> list[dict[str, Any]]:
    with session_scope() as s:
        return [
            {"id": r.id, "kind": r.kind, "status": r.status, "started_at": r.started_at.isoformat(),
             "finished_at": r.finished_at.isoformat() if r.finished_at else None,
             "resources_scanned": r.resources_scanned, "total_savings": r.total_savings, "notes": r.notes}
            for r in s.scalars(select(Run).order_by(Run.id.desc()).limit(limit))
        ]  # fmt: skip


def full_run(collect: bool = True) -> dict[str, Any]:
    """collect (every configured region) → analyze → auto-apply safe_auto (if enabled)."""
    from finops.actions.remediator import Remediator

    result: dict[str, Any] = {"collect": []}
    providers = providers_from_settings()
    if collect:
        for p in providers:
            result["collect"].append({"region": p.region, **Collector(p).collect()})
    result["analyze"] = run_analysis(provider=providers[0] if providers else None)
    result["auto_apply"] = Remediator().auto_apply(result["analyze"]["run_id"])
    return result
