"""Provider, collector and remediator against moto-mocked AWS."""

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import func, select

from finops.actions import RemediationError, Remediator
from finops.analyzer.engine import run_analysis
from finops.collector import Collector
from finops.config import Settings
from finops.db import ActionRow, MetricHourly, RecommendationRow, ResourceRow, session_scope
from finops.providers.aws import AwsProvider
from finops.providers.base import Action

REGION = "us-east-1"


@pytest.fixture
def aws():
    with mock_aws():
        yield boto3.Session(region_name=REGION)


def provider(session) -> AwsProvider:
    return AwsProvider(REGION, session=session)


def launch(session, itype="m5.2xlarge", tags=None) -> str:
    ec2 = session.client("ec2")
    tags = tags if tags is not None else {"env": "dev", "finops:managed": "true", "Name": "box"}
    resp = ec2.run_instances(
        ImageId="ami-12c6146b", MinCount=1, MaxCount=1, InstanceType=itype,
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}],
    )  # fmt: skip
    return resp["Instances"][0]["InstanceId"]


def volume(session, size=100, vtype="gp2", tags=None) -> str:
    tags = tags if tags is not None else {"env": "dev", "finops:managed": "true"}
    return session.client("ec2").create_volume(
        AvailabilityZone=f"{REGION}a", Size=size, VolumeType=vtype,
        TagSpecifications=[{"ResourceType": "volume", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}],
    )["VolumeId"]  # fmt: skip


def put_cpu(session, instance_id, value, days=10):
    cw = session.client("cloudwatch")
    now = datetime.now(UTC)
    data = [
        {"MetricName": "CPUUtilization", "Dimensions": [{"Name": "InstanceId", "Value": instance_id}],
         "Timestamp": now - timedelta(hours=h), "Value": value}
        for h in range(days * 24)
    ]  # fmt: skip
    for i in range(0, len(data), 500):
        cw.put_metric_data(Namespace="AWS/EC2", MetricData=data[i : i + 500])


def test_inventory(aws):
    iid = launch(aws)
    vid = volume(aws)
    aws.client("rds").create_db_instance(
        DBInstanceIdentifier="db1", DBInstanceClass="db.t3.micro", Engine="postgres", MasterUsername="u",
        MasterUserPassword="password123", AllocatedStorage=20, MultiAZ=True,
        Tags=[{"Key": "env", "Value": "dev"}],
    )  # fmt: skip
    p = provider(aws)
    ec2 = {r.id: r for r in p.list_compute()}
    assert ec2[iid].spec["instance_type"] == "m5.2xlarge"
    assert ec2[iid].spec["vcpu"] == 8
    assert ec2[iid].tags["finops:managed"] == "true"
    vols = {r.id: r for r in p.list_block_storage()}
    assert vols[vid].state == "available" and vols[vid].spec["size_gb"] == 100
    dbs = {r.id: r for r in p.list_databases()}
    assert dbs["db1"].spec["multi_az"] is True and dbs["db1"].env == "dev"


def test_collector_persists_inventory_and_metrics(aws):
    iid = launch(aws)
    volume(aws)
    put_cpu(aws, iid, 2.0, days=2)
    settings = Settings(lookback_days=3, cost_explorer_enabled=False, compute_optimizer_enabled=False)
    stats = Collector(provider(aws), settings).collect()
    assert stats["resources"] >= 3  # instance, its root volume, the extra volume
    with session_scope() as s:
        assert s.get(ResourceRow, iid).spec["instance_type"] == "m5.2xlarge"
        n = s.scalar(select(func.count()).select_from(MetricHourly).where(MetricHourly.resource_id == iid))
        assert n >= 40
    # second run is incremental and idempotent (upsert on the unique key)
    Collector(provider(aws), settings).collect()
    with session_scope() as s:
        n2 = s.scalar(select(func.count()).select_from(MetricHourly).where(MetricHourly.resource_id == iid))
        assert n2 == n


def test_unattached_since_is_tracked(aws):
    vid = volume(aws)
    settings = Settings(cost_explorer_enabled=False, compute_optimizer_enabled=False)
    Collector(provider(aws), settings).collect()
    with session_scope() as s:
        first = s.get(ResourceRow, vid).spec["unattached_since"]
    Collector(provider(aws), settings).collect()
    with session_scope() as s:
        assert s.get(ResourceRow, vid).spec["unattached_since"] == first


def test_describe_action_plans():
    p = AwsProvider(REGION, session=boto3.Session(region_name=REGION))
    plan = p.describe_action(Action("modify_instance_type", "i-1", "ec2", REGION, {"target_type": "m5.large"}))
    assert [s["call"] for s in plan] == [
        "stop_instances",
        "wait:instance_stopped",
        "modify_instance_attribute",
        "start_instances",
    ]
    with pytest.raises(ValueError):
        p.describe_action(Action("review_detach_volume", "vol-1", "ebs", REGION))


def test_apply_actions(aws):
    p = provider(aws)
    iid = launch(aws, "m5.xlarge")
    out = p.apply_action(Action("modify_instance_type", iid, "ec2", REGION, {"target_type": "m5.large"}))
    assert out["before"]["instance_type"] == "m5.xlarge"
    assert out["after"]["instance_type"] == "m5.large"
    assert "modify-instance-attribute" in out["rollback"]

    vid = volume(aws, size=50)
    out = p.apply_action(Action("snapshot_and_delete_volume", vid, "ebs", REGION))
    assert out["after"]["state"] == "deleted"
    snaps = aws.client("ec2").describe_snapshots(SnapshotIds=[out["snapshot_id"]])["Snapshots"]
    assert snaps[0]["VolumeId"] == vid

    vid2 = volume(aws, size=50)
    out = p.apply_action(Action("modify_volume", vid2, "ebs", REGION, {"volume_type": "gp3", "iops": 3000,
                                                                       "throughput": 125}))  # fmt: skip
    assert out["after"]["volume_type"] == "gp3"


def _seed_and_analyze(aws, tags, value=1.0, itype="t3.large"):
    iid = launch(aws, itype, tags=tags)
    put_cpu(aws, iid, value, days=10)
    settings = Settings(lookback_days=12, cost_explorer_enabled=False, compute_optimizer_enabled=False)
    Collector(provider(aws), settings).collect()
    run_analysis(provider=None)
    with session_scope() as s:
        rec = s.scalar(select(RecommendationRow).where(RecommendationRow.resource_id == iid))
        return iid, rec.id, rec.classification, rec.risk_tier


def test_remediation_flow_with_approval(aws):
    iid, rec_id, cls, tier = _seed_and_analyze(aws, {"env": "dev", "finops:managed": "true"})
    assert cls == "idle" and tier == "safe_auto"
    rem = Remediator(provider_factory=lambda region: provider(aws), settings=Settings(auto_apply=False))

    plan = rem.plan(rec_id)
    assert plan["status"] == "dry_run" and plan["plan"][0]["call"] == "stop_instances"
    # dry-run execute changes nothing
    rem.execute(plan["id"], dry_run=True)
    state = aws.client("ec2").describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]["State"]
    assert state["Name"] == "running"
    # real execute without approval is refused
    with pytest.raises(RemediationError):
        rem.execute(plan["id"], dry_run=False)

    approved = rem.approve(rec_id, approved_by="owner")
    assert approved["status"] == "approved" and approved["id"] == plan["id"]
    done = rem.execute(approved["id"], dry_run=False, actor="owner")
    assert done["status"] == "executed"
    assert done["before_state"]["state"] == "running"
    assert done["after_state"]["state"] in ("stopping", "stopped")
    assert "start-instances" in done["rollback"]
    with pytest.raises(RemediationError):
        rem.execute(done["id"], dry_run=False)  # cannot execute twice
    assert rem.mark_rolled_back(done["id"], "owner")["status"] == "rolled_back"


def test_guard_blocks_unmanaged_and_prod(aws):
    _, rec_id, _, tier = _seed_and_analyze(aws, {"env": "dev"})  # no finops:managed tag
    rem = Remediator(provider_factory=lambda region: provider(aws))
    out = rem.approve(rec_id, approved_by="owner")
    assert out["status"] == "blocked"
    assert any("finops:managed" in r for r in out["blocked_reasons"])

    _, rec_id, _, tier = _seed_and_analyze(aws, {"env": "prod", "finops:managed": "true"})
    assert tier == "manual_only"
    out = rem.approve(rec_id, approved_by="owner")
    assert out["status"] == "blocked"


def test_stale_recommendation_is_blocked(aws):
    iid, rec_id, cls, _ = _seed_and_analyze(aws, {"env": "staging", "finops:managed": "true"}, 15.0, "m5.2xlarge")
    assert cls == "over_provisioned"
    ec2 = aws.client("ec2")
    ec2.stop_instances(InstanceIds=[iid])
    ec2.modify_instance_attribute(InstanceId=iid, InstanceType={"Value": "m5.4xlarge"})  # changed by someone
    rem = Remediator(provider_factory=lambda region: provider(aws))
    action = rem.approve(rec_id, approved_by="owner")
    out = rem.execute(action["id"], dry_run=False)
    assert out["status"] == "blocked" and "instance_type" in out["blocked_reasons"][0]


def test_auto_apply_only_safe_auto(aws):
    iid, rec_id, _, tier = _seed_and_analyze(aws, {"env": "dev", "finops:managed": "true"})
    rem = Remediator(provider_factory=lambda region: provider(aws), settings=Settings(auto_apply=True))
    results = rem.auto_apply()
    assert [r["status"] for r in results] == ["executed"]
    with session_scope() as s:
        assert s.scalar(select(ActionRow.requested_by)) == "auto_apply"
    assert Remediator(settings=Settings(auto_apply=False)).auto_apply() == []
