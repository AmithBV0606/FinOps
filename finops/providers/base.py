"""Provider-neutral interface and normalised dataclasses.

Everything downstream of the collector (analyzer, savings, API, dashboard) depends only on
these types, so adding Azure/GCP means implementing ``CloudProvider`` and nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Resource:
    id: str
    provider: str
    account: str
    region: str
    type: str  # ec2 | ebs | rds
    name: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    # ec2: instance_type, vcpu, memory_gib, platform, attached_volumes
    # ebs: volume_type, size_gb, iops, throughput, attached_to, create_time, unattached_since
    # rds: instance_class, engine, multi_az, storage_gb, storage_type, vcpu, memory_gib
    spec: dict[str, Any] = field(default_factory=dict)
    state: str = ""
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    @property
    def env(self) -> str | None:
        for key in ("env", "Env", "environment", "Environment"):
            if key in self.tags:
                return self.tags[key].lower()
        return None


@dataclass
class MetricPoint:
    ts: datetime
    p50: float
    p95: float
    max: float
    sum: float


@dataclass
class MetricSeries:
    resource_id: str
    metric: str
    points: list[MetricPoint] = field(default_factory=list)
    period_seconds: int = 300  # resolution of the raw datapoints that were aggregated per hour


@dataclass
class PriceQuote:
    provider: str
    region: str
    sku_descriptor: str  # e.g. "ec2:m5.large:linux", "ebs:gp3:storage", "rds:db.t3.micro:mysql:single-az"
    unit: str  # Hrs | GB-Mo | IOPS-Mo | MiBps-Mo
    price: float
    currency: str = "USD"


@dataclass
class Action:
    action_type: str
    resource_id: str
    resource_type: str
    region: str
    params: dict[str, Any] = field(default_factory=dict)


class CloudProvider(ABC):
    name: str = "base"

    @abstractmethod
    def list_compute(self) -> list[Resource]: ...

    @abstractmethod
    def list_block_storage(self) -> list[Resource]: ...

    @abstractmethod
    def list_databases(self) -> list[Resource]: ...

    @abstractmethod
    def get_metric(
        self, resource: Resource, metric: str, start: datetime, end: datetime, period: int = 300
    ) -> MetricSeries: ...

    @abstractmethod
    def get_price(self, sku_descriptor: str, region: str) -> PriceQuote | None: ...

    @abstractmethod
    def describe_action(self, action: Action) -> list[dict[str, Any]]:
        """Return the exact API calls ``apply_action`` would make (used for dry-run)."""

    @abstractmethod
    def apply_action(self, action: Action) -> dict[str, Any]:
        """Execute ``action`` and return ``{"before": ..., "after": ..., "log": [...]}``."""
