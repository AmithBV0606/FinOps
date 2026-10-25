"""RDS rules: idle → stop, over-provisioned → step down class, Multi-AZ on dev/test → disable.

When several findings apply, the one with the largest saving becomes the recommendation and the
others are listed in ``rationale.other_findings``.
"""

from __future__ import annotations

from finops.providers import catalog
from finops.providers.base import MetricSeries, Resource
from finops.savings.engine import compute_monthly

from . import confidence
from .common import CONFIG, IDLE, INSUFFICIENT, OVER, RIGHT, Context, MetricView, Recommendation, base_rec, r
from .risk import risk_tier

CPU, CONN, FREEMEM = "CPUUtilization", "DatabaseConnections", "FreeableMemory"


def evaluate(resource: Resource, series: dict[str, MetricSeries], ctx: Context) -> Recommendation:
    rules, cfg = ctx.rules, ctx.rules.rds
    spec = resource.spec
    cls, engine, multi_az = spec.get("instance_class", ""), spec.get("engine", "mysql"), bool(spec.get("multi_az"))
    region = resource.region
    storage_monthly = round(
        float(spec.get("storage_gb") or 0)
        * ctx.prices.rds_storage_gb_month(spec.get("storage_type") or "gp2", region)
        * (2 if multi_az else 1),
        4,
    )
    instance_monthly = compute_monthly(ctx.prices.rds_hourly(cls, engine, multi_az, region))
    current = round(instance_monthly + storage_monthly, 4)

    def finish(rec: Recommendation, conf: float) -> Recommendation:
        rec.confidence = conf
        rec.risk_tier = risk_tier(resource, rec.action_type, rules)
        return rec

    if resource.state not in ("available", "backing-up", "modifying", "storage-optimization"):
        rec = base_rec(resource, RIGHT, current_monthly_cost=storage_monthly)
        rec.rationale = {"note": f"instance is {resource.state}; only storage is billed"}
        return finish(rec, 1.0)

    view = MetricView(series, ctx.now, rules.window.preferred_days)
    days = view.days(CPU)
    if days < rules.window.min_days:
        rec = base_rec(resource, INSUFFICIENT, current_monthly_cost=current)
        rec.rationale = {"days_of_data": days, "required_days": rules.window.min_days}
        return finish(rec, 0.0)

    p95_cpu, peak_conn = view.p95(CPU) or 0.0, view.peak(CONN)
    p95_conn = view.p95(CONN)
    mem_gib = spec.get("memory_gib") or (catalog.RDS_TYPES[cls].memory_gib if cls in catalog.RDS_TYPES else None)
    free_low = view.low(FREEMEM)
    free_ratio = (free_low / (mem_gib * 1024**3)) if (free_low is not None and mem_gib) else None
    conf = confidence.score(days, view.coverage(CPU), view.values(CPU), rules)
    rationale = {
        "days_of_data": days,
        "p95_cpu": r(p95_cpu),
        "p95_connections": r(p95_conn),
        "peak_connections": r(peak_conn),
        "freeable_memory_ratio_p5": r(free_ratio, 3),
        "multi_az": multi_az,
        "env": resource.env,
    }
    candidates: list[Recommendation] = []

    # ---- idle
    if days >= rules.window.rule_days and peak_conn is not None and peak_conn <= cfg.idle.max_connections:
        rec = base_rec(resource, IDLE, current_monthly_cost=current, recommended_monthly_cost=storage_monthly)
        rec.action_type = "stop_db_instance"
        rec.recommended_spec = {
            "state": "stopped",
            "follow_up": "snapshot & delete if still unused (stop lasts 7 days)",
        }
        rec.rationale = {"rule": f"0 connections for {days:.1f} d"}
        candidates.append(rec)

    # ---- Multi-AZ on non-prod
    if multi_az and (resource.env or "") in cfg.multi_az_non_prod_envs:
        single = compute_monthly(ctx.prices.rds_hourly(cls, engine, False, region)) + storage_monthly / 2
        rec = base_rec(resource, CONFIG, current_monthly_cost=current, recommended_monthly_cost=round(single, 4))
        rec.action_type = "disable_multi_az"
        rec.recommended_spec = {"instance_class": cls, "multi_az": False}
        rec.rationale = {"rule": f"Multi-AZ enabled on env={resource.env}"}
        candidates.append(rec)

    # ---- over-provisioned
    o = cfg.over_provisioned
    conn_ok = p95_conn is None or p95_conn < o.p95_connections_below
    mem_ok = free_ratio is None or free_ratio > o.min_freeable_memory_ratio
    if p95_cpu < o.p95_cpu_below and conn_ok and mem_ok and not any(c.classification == IDLE for c in candidates):
        target = catalog.step_down(cls, catalog.RDS_TYPES)
        if target:
            cur, tgt = catalog.RDS_TYPES[cls], catalog.RDS_TYPES[target]
            proj_p95 = p95_cpu * cur.vcpu / tgt.vcpu
            used_gib = mem_gib - free_low / 1024**3 if (free_ratio is not None and mem_gib) else None
            mem_fits = used_gib is None or tgt.memory_gib >= used_gib * 1.25
            hourly_t = ctx.prices.rds_hourly(target, engine, multi_az, region)
            if proj_p95 <= cfg.headroom.max_projected_p95_cpu and mem_fits and hourly_t is not None:
                rec = base_rec(
                    resource, OVER, current_monthly_cost=current,
                    recommended_monthly_cost=round(compute_monthly(hourly_t) + storage_monthly, 4),
                )  # fmt: skip
                rec.action_type = "modify_db_instance_class"
                rec.recommended_spec = {"instance_class": target, "vcpu": tgt.vcpu, "memory_gib": tgt.memory_gib}
                rec.rationale = {
                    "rule": f"p95 CPU {p95_cpu:.1f}% < {o.p95_cpu_below}%, p95 connections "
                    f"{(p95_conn or 0):.0f} < {o.p95_connections_below}, freeable memory "
                    + ("n/a" if free_ratio is None else f"{free_ratio:.0%} > {o.min_freeable_memory_ratio:.0%}"),
                    "projected_p95_cpu": r(proj_p95),
                    "projected_used_memory_gib": r(used_gib),
                }
                candidates.append(rec)
            else:
                rationale["notes"] = [f"step-down to {target} rejected by CPU/memory headroom"]

    if candidates:
        best = max(candidates, key=lambda c: c.monthly_savings)
        best.rationale = {
            **rationale,
            **best.rationale,
            "other_findings": [
                {"classification": c.classification, "action_type": c.action_type, "monthly_savings": c.monthly_savings}
                for c in candidates
                if c is not best
            ],
        }
        return finish(best, conf)

    rec = base_rec(resource, RIGHT, current_monthly_cost=current)
    rec.rationale = rationale
    return finish(rec, conf)
