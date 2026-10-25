from finops.analyzer import rules_ec2

from .conftest import make_resource
from .fixtures import ec2_series


def ec2(rid, itype="m5.2xlarge", **kw):
    return make_resource(rid, "ec2", {"instance_type": itype}, **kw)


def test_idle_instance_is_stopped(ctx):
    res = ec2("i-idle", "t3.large")
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=1, noise=0.3), net_bytes_per_5min=1000), ctx)
    assert rec.classification == "idle"
    assert rec.action_type == "stop_instance"
    assert rec.monthly_savings == round(0.0832 * 730, 4)
    assert rec.risk_tier == "safe_auto"  # dev + managed + stop
    assert rec.confidence > 0.8


def test_idle_requires_low_network(ctx):
    res = ec2("i-quiet-cpu-busy-net", "m5.large")
    # 5 MB per 5 min = 1.44 GB/day of traffic → not idle, but over-provisioned can't step down m5.large
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=1, noise=0.3), net_bytes_per_5min=5e6), ctx)
    assert rec.classification == "right_sized"
    assert any("no smaller size" in n for n in rec.rationale["notes"])


def test_over_provisioned_steps_down_one_size(ctx):
    res = ec2("i-over", "m5.2xlarge")
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=15, noise=3)), ctx)
    assert rec.classification == "over_provisioned"
    assert rec.recommended_spec["instance_type"] == "m5.xlarge"
    assert rec.monthly_savings == round((0.384 - 0.192) * 730, 4)
    assert rec.rationale["projected_p95_cpu"] <= 66
    assert rec.risk_tier == "approval"


def test_headroom_blocks_step_down_when_peaks_are_high(ctx):
    res = ec2("i-spiky", "m5.2xlarge")
    # p95 is low but daily peaks reach 60% → 120% on half the vCPUs
    rec = rules_ec2.evaluate(
        res, ec2_series(res.id, dict(base=10, noise=1, spike_every_hours=24, spike_height=50)), ctx
    )
    assert rec.classification == "right_sized"
    assert any("rejected" in n for n in rec.rationale["notes"])


def test_busy_instance_is_right_sized(ctx):
    res = ec2("i-busy", "c5.xlarge")
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=60, noise=8)), ctx)
    assert rec.classification == "right_sized"
    assert rec.monthly_savings == 0


def test_insufficient_data_under_seven_days(ctx):
    res = ec2("i-new")
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=3, noise=1), days=5), ctx)
    assert rec.classification == "insufficient_data"
    assert rec.monthly_savings == 0


def test_memory_metrics_block_over_provisioning(ctx):
    res = ec2("i-memheavy", "r5.2xlarge")
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=10, noise=2), mem=dict(base=70, noise=3)), ctx)
    assert rec.classification == "right_sized"


def test_memory_metrics_allow_when_low(ctx):
    res = ec2("i-memlow", "r5.2xlarge")
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=10, noise=2), mem=dict(base=20, noise=3)), ctx)
    assert rec.classification == "over_provisioned"
    assert rec.recommended_spec["instance_type"] == "r5.xlarge"
    assert rec.rationale["memory_metrics"] is True


def test_burstable_alternative_suggested(ctx):
    res = ec2("i-burst", "m5.xlarge")
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=6, noise=1)), ctx)
    assert rec.classification == "over_provisioned"
    alt = rec.rationale["burstable_alternative"]
    assert alt is not None and alt["instance_type"].startswith("t3.")
    assert alt["hourly_usd"] < 0.096


def test_burstable_target_respects_credit_baseline(ctx):
    # t3.large → t3.medium halves nothing (both 2 vCPU) but medium's baseline is 20% vs mean 25%
    res = ec2("i-t3", "t3.large")
    rec = rules_ec2.evaluate(res, ec2_series(res.id, dict(base=25, noise=2)), ctx)
    assert rec.classification == "right_sized"


def test_prod_and_untagged_are_manual_only(ctx):
    series_ = dict(base=15, noise=3)
    prod = ec2("i-prod", tags={"env": "prod", "finops:managed": "true"})
    untagged = ec2("i-untagged", tags={})
    assert rules_ec2.evaluate(prod, ec2_series(prod.id, series_), ctx).risk_tier == "manual_only"
    assert rules_ec2.evaluate(untagged, ec2_series(untagged.id, series_), ctx).risk_tier == "manual_only"


def test_stopped_instance_not_flagged(ctx):
    res = ec2("i-stopped", state="stopped")
    rec = rules_ec2.evaluate(res, {}, ctx)
    assert rec.classification == "right_sized"
    assert rec.current_monthly_cost == 0


def test_threshold_change_flips_classification(rules, ctx):
    res = ec2("i-edge", "m5.2xlarge")
    s = ec2_series(res.id, dict(base=30, noise=1))
    assert rules_ec2.evaluate(res, s, ctx).classification == "over_provisioned"
    rules.ec2.over_provisioned.p95_cpu_below = 25
    assert rules_ec2.evaluate(res, s, ctx).classification == "right_sized"
