"""EC2 rules: idle → stop, over-provisioned → step down one size (or burstable), else right-sized."""

from __future__ import annotations

from finops.providers import catalog
from finops.providers.base import MetricSeries, Resource
from finops.savings.engine import compute_monthly

from . import confidence
from .common import IDLE, INSUFFICIENT, OVER, RIGHT, Context, MetricView, Recommendation, base_rec, r
from .risk import risk_tier

CPU = "CPUUtilization"
MEM = "mem_used_percent"


def _shape(instance_type: str, resource: Resource) -> tuple[int | None, float | None]:
    t = catalog.EC2_TYPES.get(instance_type)
    if t:
        return t.vcpu, t.memory_gib
    if instance_type == resource.spec.get("instance_type"):
        return resource.spec.get("vcpu"), resource.spec.get("memory_gib")
    return None, None


def _burstable_alternative(
    resource: Resource, view: MetricView, ctx: Context, needed_mem: float | None, ceiling_hourly: float
) -> dict | None:
    """Cheapest T-family type whose baseline covers average CPU and whose burst covers p95."""
    current = resource.spec["instance_type"]
    family = catalog.split_type(current)[0]
    if family in catalog.BURSTABLE_FAMILIES:
        return None
    cur_vcpu, cur_mem = _shape(current, resource)
    mean_cpu, p95_cpu = view.mean(CPU), view.p95(CPU)
    if not cur_vcpu or mean_cpu is None or p95_cpu is None:
        return None
    t_family = "t4g" if family in catalog.GRAVITON_FAMILIES else "t3"
    best = None
    for t in catalog.EC2_TYPES.values():
        if t.family != t_family or t.burstable_baseline_pct is None:
            continue
        if needed_mem is not None and t.memory_gib < needed_mem:
            continue
        if needed_mem is None and cur_mem is not None and t.memory_gib < cur_mem:
            continue  # without memory metrics never shrink memory via the burstable path
        ratio = cur_vcpu / t.vcpu
        if mean_cpu * ratio > t.burstable_baseline_pct or p95_cpu * ratio > 100:
            continue
        price = ctx.prices.ec2_hourly(t.name, resource.region)
        if price is None or price >= ceiling_hourly:
            continue
        if best is None or price < best["hourly_usd"]:
            best = {
                "instance_type": t.name,
                "hourly_usd": price,
                "monthly_usd": compute_monthly(price),
                "baseline_pct": t.burstable_baseline_pct,
                "projected_mean_cpu": r(mean_cpu * ratio),
            }
    return best


def evaluate(resource: Resource, series: dict[str, MetricSeries], ctx: Context) -> Recommendation:
    rules = ctx.rules
    cfg = rules.ec2
    itype = resource.spec.get("instance_type", "")
    hourly = ctx.prices.ec2_hourly(itype, resource.region)
    current_monthly = compute_monthly(hourly)

    if resource.state != "running":
        rec = base_rec(resource, RIGHT, current_monthly_cost=0.0, confidence=1.0)
        rec.rationale = {
            "note": f"instance is {resource.state}; compute is not billed (attached EBS is analysed separately)"
        }
        rec.risk_tier = risk_tier(resource, None, rules)
        return rec

    view = MetricView(series, ctx.now, rules.window.preferred_days)
    days = view.days(CPU)
    if days < rules.window.min_days:
        rec = base_rec(resource, INSUFFICIENT, current_monthly_cost=current_monthly)
        rec.rationale = {"days_of_data": days, "required_days": rules.window.min_days}
        rec.risk_tier = risk_tier(resource, None, rules)
        return rec

    p95_cpu = view.p95(CPU) or 0.0
    peak_cpu = view.peak(CPU) or 0.0
    net_bytes = view.total("NetworkIn") + view.total("NetworkOut")
    net_mb_per_day = net_bytes / 1e6 / days
    mem_p95 = view.p95(MEM) if view.has(MEM) else None
    conf = confidence.score(days, view.coverage(CPU), view.values(CPU), rules)

    rationale = {
        "days_of_data": days,
        "p95_cpu": r(p95_cpu),
        "peak_cpu": r(peak_cpu),
        "mean_cpu": r(view.mean(CPU)),
        "network_mb_per_day": r(net_mb_per_day, 3),
        "p95_mem": r(mem_p95),
        "memory_metrics": mem_p95 is not None,
        "hourly_usd": hourly,
    }

    def finish(rec: Recommendation) -> Recommendation:
        rec.confidence = conf
        rec.rationale = {**rationale, **rec.rationale}
        rec.risk_tier = risk_tier(resource, rec.action_type, rules)
        return rec

    # ---- idle
    if p95_cpu < cfg.idle.p95_cpu_below and net_mb_per_day < cfg.idle.network_mb_per_day_below:
        rec = base_rec(resource, IDLE, current_monthly_cost=current_monthly, recommended_monthly_cost=0.0)
        rec.action_type = "stop_instance"
        rec.recommended_spec = {"state": "stopped", "follow_up": "terminate after owner review"}
        rec.rationale = {
            "rule": f"p95 CPU {p95_cpu:.2f}% < {cfg.idle.p95_cpu_below}% and network "
            f"{net_mb_per_day:.2f} MB/day < {cfg.idle.network_mb_per_day_below} MB/day",
        }
        return finish(rec)

    # ---- over-provisioned
    mem_ok = mem_p95 is None or mem_p95 < cfg.over_provisioned.p95_mem_below
    if p95_cpu < cfg.over_provisioned.p95_cpu_below and mem_ok:
        target = catalog.step_down(itype, catalog.EC2_TYPES)
        cur_vcpu, cur_mem = _shape(itype, resource)
        notes: list[str] = []
        if target and cur_vcpu:
            t_vcpu, t_mem = _shape(target, resource)
            ratio = cur_vcpu / t_vcpu if t_vcpu else 1.0
            proj_p95, proj_peak = p95_cpu * ratio, peak_cpu * ratio
            proj_mem = mem_p95 * (cur_mem / t_mem) if (mem_p95 is not None and cur_mem and t_mem) else None
            fits = proj_p95 <= cfg.headroom.max_projected_p95_cpu and proj_peak <= cfg.headroom.max_projected_peak_cpu
            if proj_mem is not None and proj_mem > 80:
                fits = False
                notes.append(f"projected memory {proj_mem:.0f}% on {target} exceeds 80%")
            target_baseline = catalog.EC2_TYPES[target].burstable_baseline_pct if target in catalog.EC2_TYPES else None
            if target_baseline is not None and (view.mean(CPU) or 0) * ratio > target_baseline:
                fits = False
                notes.append(f"projected mean CPU exceeds {target} CPU-credit baseline {target_baseline}%")
            target_hourly = ctx.prices.ec2_hourly(target, resource.region)
            if fits and target_hourly is not None and target_hourly < (hourly or 0):
                needed_mem = (cur_mem * mem_p95 / 100 * 1.25) if (mem_p95 is not None and cur_mem) else t_mem
                alt = (
                    _burstable_alternative(resource, view, ctx, needed_mem, target_hourly)
                    if cfg.suggest_burstable
                    else None
                )
                rec = base_rec(
                    resource, OVER, current_monthly_cost=current_monthly,
                    recommended_monthly_cost=compute_monthly(target_hourly),
                )  # fmt: skip
                rec.action_type = "modify_instance_type"
                rec.recommended_spec = {"instance_type": target, "vcpu": t_vcpu, "memory_gib": t_mem}
                rec.rationale = {
                    "rule": f"p95 CPU {p95_cpu:.1f}% < {cfg.over_provisioned.p95_cpu_below}%"
                    + (
                        ""
                        if mem_p95 is None
                        else f" and p95 memory {mem_p95:.1f}% < {cfg.over_provisioned.p95_mem_below}%"
                    ),
                    "projected_p95_cpu": r(proj_p95),
                    "projected_peak_cpu": r(proj_peak),
                    "projected_p95_mem": r(proj_mem),
                    "headroom_ok": True,
                    "burstable_alternative": alt,
                    "notes": notes + ([] if mem_p95 is not None else ["no memory metrics; CPU+network only"]),
                }
                return finish(rec)
            if not fits:
                notes.append(
                    f"step-down to {target} rejected: projected p95 {proj_p95:.1f}% / peak {proj_peak:.1f}% "
                    f"vs limits {cfg.headroom.max_projected_p95_cpu}% / {cfg.headroom.max_projected_peak_cpu}%"
                )
            else:
                notes.append(f"{target} is not priced or not cheaper than {itype}")
        else:
            notes.append(f"no smaller size in the {catalog.split_type(itype)[0]} family")
        rec = base_rec(resource, RIGHT, current_monthly_cost=current_monthly)
        rec.rationale = {"notes": notes}
        return finish(rec)

    rec = base_rec(resource, RIGHT, current_monthly_cost=current_monthly)
    return finish(rec)
