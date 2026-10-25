"""Agreement with AWS Compute Optimizer (§9.2) and realised-savings tracking (§9.4)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from finops.db import ActionRow, BillingSnapshot, ExternalFinding, RecommendationRow, session_scope

OUR_FLAGGED = {"idle", "over_provisioned"}
CO_FLAGGED = {"overprovisioned", "idle", "notoptimized"}


def _norm(finding: str) -> str:
    return finding.replace("_", "").replace(" ", "").lower()


def compare(ours: dict[str, dict[str, Any]], theirs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """``ours``/``theirs``: resource_id → {classification|finding, recommended_spec, resource_type}."""
    common = sorted(set(ours) & set(theirs))
    tp = fp = fn = tn = 0
    type_matches = type_compared = 0
    rows = []
    for rid in common:
        o, t = ours[rid], theirs[rid]
        o_flag = o["classification"] in OUR_FLAGGED
        t_flag = _norm(t["finding"]) in CO_FLAGGED
        tp += o_flag and t_flag
        fp += o_flag and not t_flag
        fn += t_flag and not o_flag
        tn += not o_flag and not t_flag
        o_spec = (o.get("recommended_spec") or {}).get("instance_type")
        if o_spec and t.get("recommended_spec"):
            type_compared += 1
            type_matches += o_spec == t["recommended_spec"]
        rows.append({
            "resource_id": rid, "resource_type": o.get("resource_type"), "ours": o["classification"],
            "compute_optimizer": t["finding"], "our_spec": o_spec, "co_spec": t.get("recommended_spec"),
            "our_savings": o.get("monthly_savings"), "co_savings": t.get("monthly_savings"),
            "agree": o_flag == t_flag,
        })  # fmt: skip
    n = len(common)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    return {
        "compared": n,
        "only_ours": len(set(ours) - set(theirs)),
        "only_compute_optimizer": len(set(theirs) - set(ours)),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "agreement_pct": round(100 * (tp + tn) / n, 1) if n else None,
        "instance_type_match_pct": round(100 * type_matches / type_compared, 1) if type_compared else None,
        "rows": rows,
    }


def compare_latest(run_id: int | None = None) -> dict[str, Any]:
    with session_scope() as s:
        if run_id is None:
            run_id = s.scalar(select(RecommendationRow.run_id).order_by(RecommendationRow.run_id.desc()))
        recs = s.scalars(select(RecommendationRow).where(RecommendationRow.run_id == run_id)).all()
        ours = {
            r.resource_id: {
                "classification": r.classification, "recommended_spec": r.recommended_spec,
                "resource_type": r.resource.type, "monthly_savings": r.monthly_savings,
            }
            for r in recs
            if r.classification != "insufficient_data"
        }  # fmt: skip
        theirs: dict[str, dict[str, Any]] = {}
        for f in s.scalars(select(ExternalFinding).where(ExternalFinding.source == "compute_optimizer")):
            # An "Idle" finding outranks a sizing finding for the same resource.
            if f.resource_id in theirs and _norm(theirs[f.resource_id]["finding"]) == "idle":
                continue
            theirs[f.resource_id] = {
                "finding": f.finding, "recommended_spec": f.recommended_spec,
                "monthly_savings": f.monthly_savings,
            }  # fmt: skip
    return {"run_id": run_id, **compare(ours, theirs)}


def realised_savings() -> dict[str, Any]:
    """Predicted savings of executed actions, plus tag-scoped billing snapshots (before vs after)."""
    with session_scope() as s:
        executed = s.scalars(select(ActionRow).where(ActionRow.status == "executed")).all()
        predicted = [
            {
                "action_id": a.id, "resource_id": a.recommendation.resource_id, "action_type": a.action_type,
                "executed_at": a.executed_at.isoformat() if a.executed_at else None,
                "predicted_monthly_savings": a.recommendation.monthly_savings,
            }
            for a in executed
        ]  # fmt: skip
        snaps = s.scalars(
            select(BillingSnapshot).where(BillingSnapshot.scope.like("tag:%")).order_by(BillingSnapshot.period_start)
        ).all()
        billing = [
            {"scope": b.scope, "period_start": b.period_start.isoformat(), "period_end": b.period_end.isoformat(),
             "total": b.total}
            for b in snaps
        ]  # fmt: skip
    return {
        "executed_actions": predicted,
        "predicted_total": round(sum(p["predicted_monthly_savings"] for p in predicted), 2),
        "tagged_billing": billing,
    }
