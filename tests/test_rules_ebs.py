from datetime import timedelta

import pytest

from finops.analyzer import rules_ebs

from .conftest import NOW, make_resource
from .fixtures import ebs_series


def vol(rid, vtype="gp2", size=100, state="in-use", **spec):
    return make_resource(rid, "ebs", {"volume_type": vtype, "size_gb": size, **spec}, state=state)


def test_unattached_over_seven_days_is_deleted_after_snapshot(ctx):
    res = vol("vol-orphan", size=500, state="available", unattached_since=(NOW - timedelta(days=10)).isoformat())
    rec = rules_ebs.evaluate(res, {}, ctx)
    assert rec.classification == "idle"
    assert rec.action_type == "snapshot_and_delete_volume"
    assert rec.current_monthly_cost == pytest.approx(50.0)
    assert rec.recommended_monthly_cost == pytest.approx(25.0)  # full-size snapshot upper bound
    assert rec.risk_tier == "safe_auto"


def test_recently_unattached_is_insufficient(ctx):
    res = vol("vol-new", state="available", unattached_since=(NOW - timedelta(days=2)).isoformat())
    assert rules_ebs.evaluate(res, {}, ctx).classification == "insufficient_data"


def test_unattached_falls_back_to_create_time(ctx):
    res = vol("vol-old", state="available", create_time=(NOW - timedelta(days=90)).isoformat())
    assert rules_ebs.evaluate(res, {}, ctx).classification == "idle"


def test_attached_zero_ops_needs_review(ctx):
    res = vol("vol-unused", vtype="gp3")
    rec = rules_ebs.evaluate(res, ebs_series(res.id, 0, 0), ctx)
    assert rec.classification == "idle"
    assert rec.action_type == "review_detach_volume"
    assert rec.risk_tier == "manual_only"


def test_gp2_to_gp3(ctx):
    res = vol("vol-gp2", size=200)
    rec = rules_ebs.evaluate(res, ebs_series(res.id, 3000, 3000), ctx)
    assert rec.classification == "over_provisioned"
    assert rec.action_type == "modify_volume"
    assert rec.recommended_spec == {"volume_type": "gp3", "size_gb": 200, "iops": 3000, "throughput": 125}
    assert rec.monthly_savings == pytest.approx(20.0 - 16.0)
    assert rec.risk_tier == "safe_auto"


def test_large_gp2_keeps_baseline_iops(ctx):
    res = vol("vol-big", size=2000)
    rec = rules_ebs.evaluate(res, ebs_series(res.id, 3000, 3000), ctx)
    assert rec.recommended_spec["iops"] == 6000
    assert rec.recommended_spec["throughput"] == 250
    # 2000*0.08 + 3000*0.005 + 125*0.04 = 160 + 15 + 5
    assert rec.recommended_monthly_cost == pytest.approx(180.0)
    assert rec.monthly_savings == pytest.approx(20.0)


def test_io1_with_low_iops_moves_to_gp3(ctx):
    res = vol("vol-io1", vtype="io1", size=100, iops=10000)
    # 600 ops per 5 min each for read and write → 4 IOPS
    rec = rules_ebs.evaluate(res, ebs_series(res.id, 600, 600), ctx)
    assert rec.classification == "over_provisioned"
    assert rec.recommended_spec["iops"] == 3000
    assert rec.current_monthly_cost == pytest.approx(100 * 0.125 + 10000 * 0.065)
    assert rec.recommended_monthly_cost == pytest.approx(8.0)


def test_io1_with_high_iops_stays(ctx):
    res = vol("vol-io1-busy", vtype="io1", size=1000, iops=20000)
    rec = rules_ebs.evaluate(res, ebs_series(res.id, 9000 * 300, 9000 * 300), ctx)
    assert rec.classification == "right_sized"


def test_gp3_with_activity_is_right_sized(ctx):
    res = vol("vol-gp3", vtype="gp3")
    assert rules_ebs.evaluate(res, ebs_series(res.id, 100, 100), ctx).classification == "right_sized"
