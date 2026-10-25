import pytest

from finops.analyzer import rules_rds

from .conftest import make_resource
from .fixtures import rds_series


def db(rid, cls="db.m5.xlarge", multi_az=False, tags=None, **spec):
    return make_resource(
        rid, "rds",
        {"instance_class": cls, "engine": "postgres", "multi_az": multi_az, "storage_gb": 100,
         "storage_type": "gp2", **spec},
        tags=tags,
    )  # fmt: skip


def test_idle_database_is_stopped(ctx):
    res = db("db-idle", "db.t3.medium")
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=2, connections=0, freeable_gib=3), ctx)
    assert rec.classification == "idle"
    assert rec.action_type == "stop_db_instance"
    assert rec.monthly_savings == pytest.approx(0.072 * 730)
    assert rec.risk_tier == "approval"


def test_over_provisioned_steps_down(ctx):
    res = db("db-over", "db.m5.xlarge")  # 16 GiB
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=10, connections=5, freeable_gib=12), ctx)
    assert rec.classification == "over_provisioned"
    assert rec.recommended_spec["instance_class"] == "db.m5.large"
    assert rec.monthly_savings == pytest.approx((0.356 - 0.178) * 730)


def test_low_freeable_memory_blocks_step_down(ctx):
    res = db("db-memory", "db.m5.xlarge")
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=10, connections=5, freeable_gib=4), ctx)
    assert rec.classification == "right_sized"


def test_many_connections_blocks_step_down(ctx):
    res = db("db-conns", "db.m5.xlarge")
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=10, connections=80, freeable_gib=12), ctx)
    assert rec.classification == "right_sized"


def test_multi_az_on_dev_is_disabled(ctx):
    res = db("db-dev-maz", "db.m5.large", multi_az=True)  # freeable memory low → only the config finding
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=25, connections=10, freeable_gib=2), ctx)
    assert rec.classification == "config"
    assert rec.action_type == "disable_multi_az"
    # instance halves (0.356→0.178) and Multi-AZ storage halves (23→11.5)
    assert rec.monthly_savings == pytest.approx(0.178 * 730 + 11.5)


def test_multi_az_on_prod_is_left_alone(ctx):
    res = db("db-prod-maz", "db.m5.large", multi_az=True, tags={"env": "prod"})
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=25, connections=10, freeable_gib=2), ctx)
    assert rec.classification == "right_sized"


def test_best_of_multiple_findings_wins(ctx):
    res = db("db-both", "db.m5.2xlarge", multi_az=True)
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=8, connections=5, freeable_gib=26), ctx)
    assert rec.classification == "config"  # same instance saving, but disabling Multi-AZ also halves storage
    others = rec.rationale["other_findings"]
    assert [o["classification"] for o in others] == ["over_provisioned"]


def test_occasional_connection_is_not_idle(ctx):
    res = db("db-nightly", "db.t3.medium")
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=2, connections=0, freeable_gib=3, peak_conn=1), ctx)
    assert rec.classification != "idle"


def test_insufficient_data(ctx):
    res = db("db-new")
    rec = rules_rds.evaluate(res, rds_series(res.id, cpu=5, connections=1, freeable_gib=12, days=3), ctx)
    assert rec.classification == "insufficient_data"
