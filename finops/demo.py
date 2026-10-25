"""Seed the DB with a small *synthetic* account so the API/dashboard can be explored offline.

Everything created here is fake (account ``000000000000``, ids prefixed ``demo-``); it never
touches AWS. Prices come from the static catalog.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from finops.analyzer.engine import run_analysis
from finops.collector.collector import upsert_metrics, upsert_resource
from finops.db import MetricHourly, RecommendationRow, ResourceRow, session_scope
from finops.providers import catalog
from finops.providers.base import Resource
from finops.synthetic import constant, series

ACCOUNT = "000000000000"
REGION = "us-east-1"
GB = 1024**3


def _res(rid: str, rtype: str, tags: dict, spec: dict, state: str) -> Resource:
    return Resource(id=rid, provider="aws", account=ACCOUNT, region=REGION, type=rtype,
                    name=tags.get("Name", rid), tags=tags, spec=spec, state=state)  # fmt: skip


def _ec2(rid, itype, tags, state="running"):
    t = catalog.EC2_TYPES[itype]
    return _res(rid, "ec2", tags, {"instance_type": itype, "vcpu": t.vcpu, "memory_gib": t.memory_gib}, state)


def seed_demo(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    managed_dev = {"env": "dev", "team": "platform", "finops:managed": "true"}
    resources_and_series = [
        # idle dev box → safe_auto stop
        (_ec2("demo-i-idle-dev", "t3.large", {**managed_dev, "Name": "jenkins-old"}),
         {"CPUUtilization": dict(base=1.0, noise=0.4), "NetworkIn": dict(base=2000, noise=200, clip=(0, 1e12)),
          "NetworkOut": dict(base=2000, noise=200, clip=(0, 1e12))}),
        # over-provisioned m5.2xlarge → m5.xlarge
        (_ec2("demo-i-api-staging", "m5.2xlarge", {"env": "staging", "team": "api", "finops:managed": "true",
                                                   "Name": "api-staging"}),
         {"CPUUtilization": dict(base=12, noise=3, diurnal=10), "NetworkIn": dict(base=5e7, noise=1e7, clip=(0, 1e12)),
          "NetworkOut": dict(base=5e7, noise=1e7, clip=(0, 1e12))}),
        # prod, over-provisioned → manual_only
        (_ec2("demo-i-web-prod", "c5.4xlarge", {"env": "prod", "team": "web", "Name": "web-prod"}),
         {"CPUUtilization": dict(base=15, noise=4, diurnal=8), "NetworkIn": dict(base=2e8, noise=3e7, clip=(0, 1e12)),
          "NetworkOut": dict(base=2e8, noise=3e7, clip=(0, 1e12))}),
        # busy → right_sized
        (_ec2("demo-i-batch", "c5.xlarge", {"env": "prod", "team": "data", "Name": "batch"}),
         {"CPUUtilization": dict(base=55, noise=10, diurnal=20), "NetworkIn": dict(base=1e8, noise=1e7, clip=(0, 1e12)),
          "NetworkOut": dict(base=1e8, noise=1e7, clip=(0, 1e12))}),
        # unattached, 20 days → snapshot+delete
        (_res("demo-vol-orphan", "ebs", {**managed_dev, "Name": "old-data"},
              {"volume_type": "gp2", "size_gb": 500, "attached_to": [],
               "create_time": (now - timedelta(days=60)).isoformat(),
               "unattached_since": (now - timedelta(days=20)).isoformat()}, "available"), {}),
        # gp2 in use → gp3
        (_res("demo-vol-gp2", "ebs", {"env": "staging", "team": "api", "finops:managed": "true"},
              {"volume_type": "gp2", "size_gb": 200, "attached_to": ["demo-i-api-staging"]}, "in-use"),
         {"VolumeReadOps": dict(base=3000, noise=500, clip=(0, 1e12)),
          "VolumeWriteOps": dict(base=6000, noise=800, clip=(0, 1e12))}),
        # dev RDS with Multi-AZ
        (_res("demo-db-dev", "rds", {**managed_dev},
              {"instance_class": "db.m5.large", "engine": "postgres", "multi_az": True, "storage_gb": 100,
               "storage_type": "gp2"}, "available"),
         {"CPUUtilization": dict(base=20, noise=5), "DatabaseConnections": dict(base=8, noise=2, clip=(0, 1e6)),
          "FreeableMemory": dict(base=3 * GB, noise=0.2 * GB, clip=(0, 1e13))}),
        # idle RDS (no connections)
        (_res("demo-db-forgotten", "rds", {"env": "test", "team": "data", "finops:managed": "true"},
              {"instance_class": "db.t3.medium", "engine": "mysql", "multi_az": False, "storage_gb": 50,
               "storage_type": "gp2"}, "available"),
         {"CPUUtilization": dict(base=2, noise=0.5), "DatabaseConnections": dict(base=0, noise=0, clip=(0, 0)),
          "FreeableMemory": dict(base=3 * GB, noise=0.1 * GB, clip=(0, 1e13))}),
        # new instance: only 3 days of data
        (_ec2("demo-i-new", "m5.large", {"env": "dev", "team": "ml", "Name": "notebook"}),
         {"CPUUtilization": dict(base=5, noise=2, days=3)}),
    ]  # fmt: skip
    with session_scope() as s:
        demo_ids = s.scalars(select(ResourceRow.id).where(ResourceRow.id.like("demo-%"))).all()
        s.execute(delete(MetricHourly).where(MetricHourly.resource_id.in_(demo_ids)))
        s.execute(delete(RecommendationRow).where(RecommendationRow.resource_id.in_(demo_ids)))
        for i, (res, metrics) in enumerate(resources_and_series):
            upsert_resource(s, res, now)
            for j, (metric, kw) in enumerate(metrics.items()):
                kw = {"days": 30, **kw}
                ms = (constant(res.id, metric, kw["base"], kw["days"], end=now) if kw.get("noise") == 0
                      else series(res.id, metric, end=now, seed=100 * i + j, **kw))  # fmt: skip
                upsert_metrics(s, ms, now)
    return run_analysis(provider=None, now=now)
