"""EBS rules: unattached → snapshot+delete, attached-but-unused → review, gp2/io1 → gp3."""

from __future__ import annotations

import math

from finops.providers.base import MetricSeries, Resource
from finops.savings.engine import ebs_monthly, gp2_equivalent_gp3

from . import confidence
from .common import (
    IDLE,
    INSUFFICIENT,
    OVER,
    RIGHT,
    Context,
    MetricView,
    Recommendation,
    base_rec,
    parse_ts,
    r,
)
from .risk import risk_tier

READ, WRITE = "VolumeReadOps", "VolumeWriteOps"


def evaluate(resource: Resource, series: dict[str, MetricSeries], ctx: Context) -> Recommendation:
    rules, cfg = ctx.rules, ctx.rules.ebs
    spec = resource.spec
    vtype, size = spec.get("volume_type", "gp2"), float(spec.get("size_gb") or 0)
    current = ebs_monthly(ctx.prices, resource.region, vtype, size, spec.get("iops"), spec.get("throughput"))
    snapshot_cost = round(size * ctx.prices.snapshot_gb_month(resource.region), 4)

    def finish(rec: Recommendation, conf: float) -> Recommendation:
        rec.confidence = round(conf, 3)
        rec.risk_tier = risk_tier(resource, rec.action_type, rules)
        return rec

    # ---- unattached
    if resource.state == "available":
        since = parse_ts(spec.get("unattached_since")) or parse_ts(spec.get("create_time")) or ctx.now
        days = (ctx.now - since).total_seconds() / 86400
        if days >= cfg.unattached_days:
            rec = base_rec(resource, IDLE, current_monthly_cost=current, recommended_monthly_cost=snapshot_cost)
            rec.action_type = "snapshot_and_delete_volume"
            rec.recommended_spec = {"state": "deleted", "backup": "snapshot", "snapshot_monthly_usd": snapshot_cost}
            rec.rationale = {
                "rule": f"unattached for {days:.1f} d > {cfg.unattached_days} d",
                "unattached_days": r(days, 1),
                "note": "savings net of a full-size snapshot (upper bound; snapshots are incremental)",
            }
            return finish(rec, min(1.0, days / (2 * cfg.unattached_days)))
        rec = base_rec(resource, INSUFFICIENT, current_monthly_cost=current)
        rec.rationale = {"unattached_days": r(days, 1), "required_days": cfg.unattached_days}
        return finish(rec, 0.0)

    view = MetricView(series, ctx.now, rules.window.preferred_days)
    days = max(view.days(READ), view.days(WRITE))
    ops_total = view.total(READ) + view.total(WRITE)
    ops_per_day = ops_total / days if days else None
    period = view.period(READ)
    p95_iops = None
    if view.has(READ) or view.has(WRITE):
        # hourly p95 of per-period op counts / period seconds ≈ p95 IOPS; read+write summed (upper bound)
        p95_iops = ((view.p95(READ) or 0) + (view.p95(WRITE) or 0)) / period
    rationale = {
        "days_of_data": days,
        "ops_per_day": r(ops_per_day, 1),
        "p95_iops": r(p95_iops, 1),
        "volume_type": vtype,
        "size_gb": size,
    }
    conf_metrics = (
        confidence.score(days, max(view.coverage(READ), view.coverage(WRITE)), view.values(READ), rules)
        if days
        else 0.0
    )

    # ---- attached but unused
    if days >= cfg.zero_ops_days and ops_per_day is not None and ops_per_day < cfg.zero_ops_per_day_below:
        rec = base_rec(resource, IDLE, current_monthly_cost=current, recommended_monthly_cost=snapshot_cost)
        rec.action_type = "review_detach_volume"
        rec.recommended_spec = {"state": "detach → snapshot → delete (after owner review)"}
        rec.rationale = {
            **rationale,
            "rule": f"attached but {ops_per_day:.2f} ops/day < {cfg.zero_ops_per_day_below} for {days:.1f} d",
        }
        return finish(rec, conf_metrics)

    # ---- migrate to gp3
    if vtype in cfg.migrate_to_gp3:
        if vtype == "gp2":
            iops, throughput = gp2_equivalent_gp3(size)
            reason = "gp2 → gp3 at equal baseline IOPS/throughput"
            conf = 0.95  # pure price change, no utilisation assumption
        else:
            if p95_iops is None or days < rules.window.min_days:
                rec = base_rec(resource, INSUFFICIENT, current_monthly_cost=current)
                rec.rationale = {**rationale, "required_days": rules.window.min_days}
                return finish(rec, 0.0)
            if p95_iops > cfg.io1_max_p95_iops_for_gp3:
                rec = base_rec(resource, RIGHT, current_monthly_cost=current)
                rec.rationale = {**rationale, "note": "IOPS demand exceeds gp3 maximum"}
                return finish(rec, conf_metrics)
            provisioned = spec.get("iops") or 3000
            iops = int(max(3000, min(provisioned, math.ceil(p95_iops * 1.2))))
            throughput = 125
            reason = f"{vtype} p95 IOPS {p95_iops:.0f} fits gp3 (provisioning {iops} IOPS incl. 20% headroom)"
            conf = conf_metrics
        target = ebs_monthly(ctx.prices, resource.region, "gp3", size, iops, throughput)
        if target < current:
            rec = base_rec(resource, OVER, current_monthly_cost=current, recommended_monthly_cost=target)
            rec.action_type = "modify_volume"
            rec.recommended_spec = {"volume_type": "gp3", "size_gb": size, "iops": iops, "throughput": throughput}
            rec.rationale = {**rationale, "rule": reason}
            return finish(rec, conf)

    rec = base_rec(resource, RIGHT if days >= rules.window.min_days else INSUFFICIENT, current_monthly_cost=current)
    rec.rationale = rationale
    return finish(rec, conf_metrics)
