"""AWS implementation of ``CloudProvider`` (boto3).

Two sessions are used in practice:

* the collector builds this with the read-only profile (``finops-ro``);
* the remediator builds a second instance with the tag-scoped action profile (``finops-rw``)
  and calls only ``apply_action``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import catalog
from .aggregate import aggregate_hourly
from .base import Action, CloudProvider, MetricSeries, PriceQuote, Resource

log = logging.getLogger(__name__)

# (namespace, dimension, CloudWatch statistic) per resource type and metric
METRICS: dict[str, dict[str, tuple[str, str, str]]] = {
    "ec2": {
        "CPUUtilization": ("AWS/EC2", "InstanceId", "Average"),
        "NetworkIn": ("AWS/EC2", "InstanceId", "Sum"),
        "NetworkOut": ("AWS/EC2", "InstanceId", "Sum"),
        "EBSReadOps": ("AWS/EC2", "InstanceId", "Sum"),
        "EBSWriteOps": ("AWS/EC2", "InstanceId", "Sum"),
        "mem_used_percent": ("CWAgent", "InstanceId", "Average"),  # only if CloudWatch agent installed
    },
    "ebs": {
        "VolumeReadOps": ("AWS/EBS", "VolumeId", "Sum"),
        "VolumeWriteOps": ("AWS/EBS", "VolumeId", "Sum"),
        "VolumeIdleTime": ("AWS/EBS", "VolumeId", "Sum"),
    },
    "rds": {
        "CPUUtilization": ("AWS/RDS", "DBInstanceIdentifier", "Average"),
        "DatabaseConnections": ("AWS/RDS", "DBInstanceIdentifier", "Average"),
        "FreeableMemory": ("AWS/RDS", "DBInstanceIdentifier", "Average"),
        "ReadIOPS": ("AWS/RDS", "DBInstanceIdentifier", "Average"),
        "WriteIOPS": ("AWS/RDS", "DBInstanceIdentifier", "Average"),
        "FreeStorageSpace": ("AWS/RDS", "DBInstanceIdentifier", "Average"),
    },
}

RDS_ENGINE_NAMES = {"mysql": "MySQL", "postgres": "PostgreSQL", "mariadb": "MariaDB"}

AUTOMATABLE_ACTIONS = {
    "stop_instance",
    "modify_instance_type",
    "snapshot_and_delete_volume",
    "modify_volume",
    "modify_db_instance_class",
    "stop_db_instance",
    "disable_multi_az",
}


def _tags(tag_list: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in (tag_list or [])}


class AwsProvider(CloudProvider):
    name = "aws"

    def __init__(self, region: str, profile: str | None = None, session: boto3.Session | None = None):
        self.region = region
        self.session = session or boto3.Session(profile_name=profile, region_name=region)
        self._clients: dict[str, Any] = {}
        self._account: str | None = None

    def client(self, service: str, region: str | None = None):
        key = f"{service}:{region or self.region}"
        if key not in self._clients:
            self._clients[key] = self.session.client(service, region_name=region or self.region)
        return self._clients[key]

    @property
    def account(self) -> str:
        if self._account is None:
            try:
                self._account = self.client("sts").get_caller_identity()["Account"]
            except (ClientError, BotoCoreError):
                self._account = "unknown"
        return self._account

    # ------------------------------------------------------------------ inventory
    def _instance_type_shapes(self, types: set[str]) -> dict[str, tuple[int, float]]:
        shapes: dict[str, tuple[int, float]] = {}
        missing = [t for t in types if t not in catalog.EC2_TYPES]
        for t in types - set(missing):
            shapes[t] = (catalog.EC2_TYPES[t].vcpu, catalog.EC2_TYPES[t].memory_gib)
        for i in range(0, len(missing), 100):
            try:
                resp = self.client("ec2").describe_instance_types(InstanceTypes=missing[i : i + 100])
            except ClientError as exc:
                log.warning("describe_instance_types failed: %s", exc)
                continue
            for it in resp["InstanceTypes"]:
                vcpu = it["VCpuInfo"]["DefaultVCpus"]
                mem = it["MemoryInfo"]["SizeInMiB"] / 1024
                shapes[it["InstanceType"]] = (vcpu, mem)
                catalog.register_instance_type(it["InstanceType"], vcpu, mem)
        return shapes

    def list_compute(self) -> list[Resource]:
        raw = []
        for page in self.client("ec2").get_paginator("describe_instances").paginate():
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    if inst["State"]["Name"] in ("terminated", "shutting-down"):
                        continue
                    raw.append(inst)
        shapes = self._instance_type_shapes({i["InstanceType"] for i in raw})
        out = []
        for inst in raw:
            tags = _tags(inst.get("Tags"))
            vcpu, mem = shapes.get(inst["InstanceType"], (None, None))
            out.append(
                Resource(
                    id=inst["InstanceId"],
                    provider=self.name,
                    account=self.account,
                    region=self.region,
                    type="ec2",
                    name=tags.get("Name", ""),
                    tags=tags,
                    state=inst["State"]["Name"],
                    spec={
                        "instance_type": inst["InstanceType"],
                        "vcpu": vcpu,
                        "memory_gib": mem,
                        "platform": inst.get("PlatformDetails", "Linux/UNIX"),
                        "launch_time": inst.get("LaunchTime").isoformat() if inst.get("LaunchTime") else None,
                        "attached_volumes": [
                            m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", []) if "Ebs" in m
                        ],
                        "lifecycle": inst.get("InstanceLifecycle", "on-demand"),
                    },
                )
            )
        return out

    def list_block_storage(self) -> list[Resource]:
        out = []
        for page in self.client("ec2").get_paginator("describe_volumes").paginate():
            for vol in page["Volumes"]:
                tags = _tags(vol.get("Tags"))
                attachments = [a["InstanceId"] for a in vol.get("Attachments", [])]
                out.append(
                    Resource(
                        id=vol["VolumeId"],
                        provider=self.name,
                        account=self.account,
                        region=self.region,
                        type="ebs",
                        name=tags.get("Name", ""),
                        tags=tags,
                        state=vol["State"],
                        spec={
                            "volume_type": vol["VolumeType"],
                            "size_gb": vol["Size"],
                            "iops": vol.get("Iops"),
                            "throughput": vol.get("Throughput"),
                            "attached_to": attachments,
                            "create_time": vol["CreateTime"].isoformat() if vol.get("CreateTime") else None,
                            "snapshot_id": vol.get("SnapshotId") or None,
                        },
                    )
                )
        return out

    def list_databases(self) -> list[Resource]:
        out = []
        for page in self.client("rds").get_paginator("describe_db_instances").paginate():
            for db in page["DBInstances"]:
                if db.get("Engine", "").startswith("aurora"):
                    continue  # Aurora is billed per cluster; out of scope
                tags = _tags(db.get("TagList"))
                shape = catalog.RDS_TYPES.get(db["DBInstanceClass"])
                out.append(
                    Resource(
                        id=db["DBInstanceIdentifier"],
                        provider=self.name,
                        account=self.account,
                        region=self.region,
                        type="rds",
                        name=db["DBInstanceIdentifier"],
                        tags=tags,
                        state=db["DBInstanceStatus"],
                        spec={
                            "instance_class": db["DBInstanceClass"],
                            "engine": db["Engine"],
                            "engine_version": db.get("EngineVersion"),
                            "multi_az": bool(db.get("MultiAZ")),
                            "storage_gb": db.get("AllocatedStorage"),
                            "storage_type": db.get("StorageType"),
                            "vcpu": shape.vcpu if shape else None,
                            "memory_gib": shape.memory_gib if shape else None,
                            "arn": db.get("DBInstanceArn"),
                        },
                    )
                )
        return out

    # ------------------------------------------------------------------ metrics
    def get_metric(
        self, resource: Resource, metric: str, start: datetime, end: datetime, period: int = 300
    ) -> MetricSeries:
        namespace, dimension, stat = METRICS[resource.type][metric]
        query = {
            "Id": "m0",
            "MetricStat": {
                "Metric": {
                    "Namespace": namespace,
                    "MetricName": metric,
                    "Dimensions": [{"Name": dimension, "Value": resource.id}],
                },
                "Period": period,
                "Stat": stat,
            },
            "ReturnData": True,
        }
        datapoints: list[tuple[datetime, float]] = []
        paginator = self.client("cloudwatch").get_paginator("get_metric_data")
        for page in paginator.paginate(MetricDataQueries=[query], StartTime=start, EndTime=end):
            for result in page["MetricDataResults"]:
                datapoints.extend(zip(result["Timestamps"], result["Values"], strict=False))
        return MetricSeries(resource.id, metric, aggregate_hourly(datapoints), period)

    # ------------------------------------------------------------------ pricing
    def _price_filters(self, sku: str, region: str) -> tuple[str, list[dict[str, str]]] | None:
        parts = sku.split(":")
        f = lambda k, v: {"Type": "TERM_MATCH", "Field": k, "Value": v}  # noqa: E731
        if parts[0] == "ec2":
            return "AmazonEC2", [
                f("instanceType", parts[1]),
                f("regionCode", region),
                f("operatingSystem", "Linux"),
                f("tenancy", "Shared"),
                f("preInstalledSw", "NA"),
                f("capacitystatus", "Used"),
            ]
        if parts[0] == "ebs":
            family = {"storage": "Storage", "iops": "System Operation", "throughput": "Provisioned Throughput"}
            return "AmazonEC2", [
                f("productFamily", family[parts[2]]),
                f("volumeApiName", parts[1]),
                f("regionCode", region),
            ]
        if parts[0] == "rds":
            engine = RDS_ENGINE_NAMES.get(catalog.normalise_engine(parts[2]), parts[2])
            return "AmazonRDS", [
                f("instanceType", parts[1]),
                f("regionCode", region),
                f("databaseEngine", engine),
                f("deploymentOption", "Multi-AZ" if parts[3] == "multi-az" else "Single-AZ"),
            ]
        return None

    def get_price(self, sku_descriptor: str, region: str) -> PriceQuote | None:
        spec = self._price_filters(sku_descriptor, region)
        if spec is None:
            return None
        service, filters = spec
        try:
            # The Pricing API is only served from a few regions; us-east-1 always works.
            resp = self.client("pricing", "us-east-1").get_products(ServiceCode=service, Filters=filters, MaxResults=20)
        except (ClientError, BotoCoreError) as exc:
            log.warning("pricing lookup failed for %s: %s", sku_descriptor, exc)
            return None
        for item in resp.get("PriceList", []):
            product = json.loads(item) if isinstance(item, str) else item
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    usd = float(dim.get("pricePerUnit", {}).get("USD", 0) or 0)
                    if usd > 0:
                        return PriceQuote(self.name, region, sku_descriptor, dim.get("unit", ""), usd)
        return None

    # ------------------------------------------------------------------ billing / validation
    def get_cost_by_service(self, start: datetime, end: datetime) -> dict[str, float]:
        """Cost Explorer spend grouped by service ($0.01 per call — callers must cache)."""
        resp = self.client("ce", "us-east-1").get_cost_and_usage(
            TimePeriod={"Start": start.strftime("%Y-%m-%d"), "End": end.strftime("%Y-%m-%d")},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        totals: dict[str, float] = {}
        for period in resp["ResultsByTime"]:
            for group in period["Groups"]:
                service = group["Keys"][0]
                totals[service] = totals.get(service, 0.0) + float(group["Metrics"]["UnblendedCost"]["Amount"])
        return totals

    def get_cost_for_tag(self, start: datetime, end: datetime, key: str, value: str) -> float:
        resp = self.client("ce", "us-east-1").get_cost_and_usage(
            TimePeriod={"Start": start.strftime("%Y-%m-%d"), "End": end.strftime("%Y-%m-%d")},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Tags": {"Key": key, "Values": [value]}},
        )
        return sum(float(p["Total"]["UnblendedCost"]["Amount"]) for p in resp["ResultsByTime"])

    def get_compute_optimizer_findings(self) -> list[dict[str, Any]]:
        """EC2, EBS and idle findings from Compute Optimizer, normalised for comparison."""
        co = self.client("compute-optimizer")
        findings: list[dict[str, Any]] = []
        calls = [
            ("get_ec2_instance_recommendations", "instanceRecommendations", "ec2"),
            ("get_ebs_volume_recommendations", "volumeRecommendations", "ebs"),
        ]
        for method, key, rtype in calls:
            token = None
            while True:
                kwargs = {"nextToken": token} if token else {}
                try:
                    resp = getattr(co, method)(**kwargs)
                except ClientError as exc:
                    log.warning("compute optimizer %s failed: %s", method, exc)
                    break
                for rec in resp.get(key, []):
                    arn = rec.get("instanceArn") or rec.get("volumeArn", "")
                    options = rec.get("recommendationOptions") or rec.get("volumeRecommendationOptions") or []
                    best = options[0] if options else {}
                    savings = (best.get("savingsOpportunity") or {}).get("estimatedMonthlySavings") or {}
                    findings.append(
                        {
                            "resource_id": arn.rsplit("/", 1)[-1],
                            "resource_type": rtype,
                            "finding": rec.get("finding", ""),
                            "recommended_spec": best.get("instanceType")
                            or (best.get("configuration") or {}).get("volumeType"),
                            "monthly_savings": float(savings.get("value") or 0),
                        }
                    )
                token = resp.get("nextToken")
                if not token:
                    break
        # Idle recommendations API (EC2, EBS, RDS) — newer; absent on older botocore.
        if hasattr(co, "get_idle_recommendations"):
            try:
                for rec in co.get_idle_recommendations().get("idleRecommendations", []):
                    savings = (rec.get("savingsOpportunity") or {}).get("estimatedMonthlySavings") or {}
                    findings.append(
                        {
                            "resource_id": rec.get("resourceId") or rec.get("resourceArn", "").rsplit("/", 1)[-1],
                            "resource_type": {"EC2Instance": "ec2", "EBSVolume": "ebs", "RDSDBInstance": "rds"}.get(
                                rec.get("resourceType", ""), rec.get("resourceType", "")
                            ),
                            "finding": "Idle",
                            "recommended_spec": None,
                            "monthly_savings": float(savings.get("value") or 0),
                        }
                    )
            except ClientError as exc:
                log.warning("compute optimizer idle recommendations failed: %s", exc)
        return findings

    # ------------------------------------------------------------------ actions
    def describe_action(self, action: Action) -> list[dict[str, Any]]:
        p = action.params
        rid = action.resource_id
        plans = {
            "stop_instance": [{"service": "ec2", "call": "stop_instances", "args": {"InstanceIds": [rid]}}],
            "modify_instance_type": [
                {"service": "ec2", "call": "stop_instances", "args": {"InstanceIds": [rid]}},
                {"service": "ec2", "call": "wait:instance_stopped", "args": {"InstanceIds": [rid]}},
                {
                    "service": "ec2",
                    "call": "modify_instance_attribute",
                    "args": {"InstanceId": rid, "InstanceType": {"Value": p.get("target_type")}},
                },
                {"service": "ec2", "call": "start_instances", "args": {"InstanceIds": [rid]}},
            ],
            "snapshot_and_delete_volume": [
                {
                    "service": "ec2",
                    "call": "create_snapshot",
                    "args": {"VolumeId": rid, "Description": f"finops pre-delete backup of {rid}"},
                },
                {"service": "ec2", "call": "wait:snapshot_completed", "args": {"SnapshotIds": ["<new>"]}},
                {"service": "ec2", "call": "delete_volume", "args": {"VolumeId": rid}},
            ],
            "modify_volume": [
                {
                    "service": "ec2",
                    "call": "modify_volume",
                    "args": {
                        k: v
                        for k, v in {
                            "VolumeId": rid,
                            "VolumeType": p.get("volume_type", "gp3"),
                            "Iops": p.get("iops"),
                            "Throughput": p.get("throughput"),
                        }.items()
                        if v is not None
                    },
                }
            ],  # fmt: skip
            "modify_db_instance_class": [
                {
                    "service": "rds",
                    "call": "modify_db_instance",
                    "args": {
                        "DBInstanceIdentifier": rid,
                        "DBInstanceClass": p.get("target_class"),
                        "ApplyImmediately": bool(p.get("apply_immediately", False)),
                    },
                }
            ],
            "stop_db_instance": [{"service": "rds", "call": "stop_db_instance", "args": {"DBInstanceIdentifier": rid}}],
            "disable_multi_az": [
                {
                    "service": "rds",
                    "call": "modify_db_instance",
                    "args": {
                        "DBInstanceIdentifier": rid,
                        "MultiAZ": False,
                        "ApplyImmediately": bool(p.get("apply_immediately", False)),
                    },
                }
            ],
        }
        if action.action_type not in plans:
            raise ValueError(f"action {action.action_type!r} is not automatable (manual review only)")
        return plans[action.action_type]

    def _snapshot_state(self, action: Action) -> dict[str, Any]:
        rid = action.resource_id
        try:
            if action.resource_type == "ec2":
                inst = self.client("ec2").describe_instances(InstanceIds=[rid])["Reservations"][0]["Instances"][0]
                return {"state": inst["State"]["Name"], "instance_type": inst["InstanceType"]}
            if action.resource_type == "ebs":
                vols = self.client("ec2").describe_volumes(VolumeIds=[rid])["Volumes"]
                if not vols:
                    return {"state": "deleted"}
                v = vols[0]
                return {"state": v["State"], "volume_type": v["VolumeType"], "size_gb": v["Size"],
                        "iops": v.get("Iops"), "throughput": v.get("Throughput")}  # fmt: skip
            if action.resource_type == "rds":
                db = self.client("rds").describe_db_instances(DBInstanceIdentifier=rid)["DBInstances"][0]
                return {"state": db["DBInstanceStatus"], "instance_class": db["DBInstanceClass"],
                        "multi_az": db.get("MultiAZ")}  # fmt: skip
        except ClientError as exc:
            if "NotFound" in str(exc):
                return {"state": "deleted"}
            raise
        return {}

    def apply_action(self, action: Action) -> dict[str, Any]:
        steps = self.describe_action(action)
        before = self._snapshot_state(action)
        logs: list[str] = []
        extra: dict[str, Any] = {}
        for step in steps:
            client = self.client(step["service"])
            call, args = step["call"], dict(step["args"])
            if call.startswith("wait:"):
                if "SnapshotIds" in args:
                    args["SnapshotIds"] = [extra["snapshot_id"]]
                client.get_waiter(call.split(":", 1)[1]).wait(**args, WaiterConfig={"Delay": 15, "MaxAttempts": 80})
                logs.append(f"waited {call}")
                continue
            resp = getattr(client, call)(**args)
            if call == "create_snapshot":
                extra["snapshot_id"] = resp["SnapshotId"]
                self.client("ec2").create_tags(
                    Resources=[resp["SnapshotId"]],
                    Tags=[{"Key": "finops:source-volume", "Value": action.resource_id}],
                )
            logs.append(f"{step['service']}.{call}({json.dumps(args, default=str)}) ok")
        after = self._snapshot_state(action)
        return {"before": before, "after": after, "log": logs, "rollback": self.rollback_notes(action, before, extra),
                **extra}  # fmt: skip

    @staticmethod
    def rollback_notes(action: Action, before: dict[str, Any], extra: dict[str, Any]) -> str:
        rid = action.resource_id
        t = action.action_type
        if t == "stop_instance":
            return f"aws ec2 start-instances --instance-ids {rid}"
        if t == "modify_instance_type":
            return (f"stop {rid}, then aws ec2 modify-instance-attribute --instance-id {rid} "
                    f"--instance-type {before.get('instance_type')}, then start")  # fmt: skip
        if t == "snapshot_and_delete_volume":
            return (f"aws ec2 create-volume --snapshot-id {extra.get('snapshot_id')} --volume-type "
                    f"{before.get('volume_type')} --availability-zone <az>")  # fmt: skip
        if t == "modify_volume":
            return (f"aws ec2 modify-volume --volume-id {rid} --volume-type {before.get('volume_type')} "
                    "(allowed after the 6-hour modification cooldown)")  # fmt: skip
        if t == "modify_db_instance_class":
            return (f"aws rds modify-db-instance --db-instance-identifier {rid} --db-instance-class "
                    f"{before.get('instance_class')} --apply-immediately")  # fmt: skip
        if t == "stop_db_instance":
            return f"aws rds start-db-instance --db-instance-identifier {rid} (AWS auto-starts after 7 days)"
        if t == "disable_multi_az":
            return f"aws rds modify-db-instance --db-instance-identifier {rid} --multi-az --apply-immediately"
        return ""


def now_utc() -> datetime:
    return datetime.now(UTC)
