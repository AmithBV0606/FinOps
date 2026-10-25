"""Collector (§4.2): inventory + metrics + prices (+ optional billing/Compute Optimizer) → DB.

Metrics are append-only and fetched incrementally: each run pulls from the last stored hour
(re-fetching it, since it may have been partial) up to now, capped at ``lookback_days``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from finops.config import Settings, get_settings
from finops.db import BillingSnapshot, ExternalFinding, MetricHourly, ResourceRow, Run, session_scope
from finops.providers import catalog
from finops.providers.aws import METRICS, AwsProvider
from finops.providers.base import CloudProvider, MetricSeries, Resource
from finops.savings.pricing import PriceBook

log = logging.getLogger(__name__)

COLLECT_STATES = {"ec2": {"running"}, "ebs": {"in-use"}, "rds": {"available", "backing-up", "modifying"}}


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def upsert_resource(session, res: Resource, now: datetime) -> ResourceRow:
    row = session.get(ResourceRow, res.id)
    spec = dict(res.spec)
    if res.type == "ebs":
        prev = (row.spec or {}) if row else {}
        if res.state == "available":
            # First sighting of an unattached volume: we cannot know when it was detached, so fall back
            # to its creation time if we have never seen it before, otherwise start the clock now.
            spec["unattached_since"] = prev.get("unattached_since") or (
                spec.get("create_time") if row is None else now.isoformat()
            )
        else:
            spec.pop("unattached_since", None)
    if row is None:
        row = ResourceRow(id=res.id, first_seen=now)
        session.add(row)
    row.provider, row.account, row.region, row.type = res.provider, res.account, res.region, res.type
    row.name, row.tags, row.spec, row.state, row.last_seen = res.name, res.tags, spec, res.state, now
    return row


def upsert_metrics(session, series: MetricSeries, now: datetime) -> int:
    if not series.points:
        return 0
    values = [
        {"resource_id": series.resource_id, "metric": series.metric, "ts": p.ts, "p50": p.p50, "p95": p.p95,
         "max": p.max, "sum": p.sum, "period_seconds": series.period_seconds, "collected_at": now}
        for p in series.points
    ]  # fmt: skip
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(MetricHourly).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["resource_id", "metric", "ts"],
        set_={k: stmt.excluded[k] for k in ("p50", "p95", "max", "sum", "collected_at")},
    )
    session.execute(stmt)
    return len(values)


class Collector:
    def __init__(self, provider: CloudProvider, settings: Settings | None = None):
        self.provider = provider
        self.settings = settings or get_settings()

    def collect(
        self,
        with_billing: bool | None = None,
        with_compute_optimizer: bool | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        stats: dict[str, Any] = {"resources": 0, "metric_points": 0, "errors": []}
        with session_scope() as s:
            run = Run(kind="collect", started_at=now)
            s.add(run)
            s.flush()
            run_id = run.id

        resources = self.collect_inventory(now, stats)
        self.collect_metrics(resources, now, stats)
        self.warm_prices(resources, stats)
        if with_billing if with_billing is not None else self.settings.cost_explorer_enabled:
            self.collect_billing(now, stats)
        if with_compute_optimizer if with_compute_optimizer is not None else self.settings.compute_optimizer_enabled:
            self.collect_compute_optimizer(now, stats)

        with session_scope() as s:
            run = s.get(Run, run_id)
            run.finished_at = datetime.now(UTC)
            run.resources_scanned = stats["resources"]
            run.status = "ok" if not stats["errors"] else "partial"
            run.notes = "; ".join(stats["errors"])[:2000]
        stats["run_id"] = run_id
        return stats

    # ------------------------------------------------------------------ steps
    def collect_inventory(self, now: datetime, stats: dict[str, Any]) -> list[Resource]:
        resources: list[Resource] = []
        for name, fn in (
            ("ec2", self.provider.list_compute),
            ("ebs", self.provider.list_block_storage),
            ("rds", self.provider.list_databases),
        ):
            try:
                found = fn()
            except Exception as exc:  # keep collecting other services
                log.exception("inventory %s failed", name)
                stats["errors"].append(f"inventory {name}: {exc}")
                continue
            resources.extend(found)
            stats[f"{name}_count"] = len(found)
        with session_scope() as s:
            for res in resources:
                upsert_resource(s, res, now)
        stats["resources"] = len(resources)
        return resources

    def collect_metrics(self, resources: list[Resource], now: datetime, stats: dict[str, Any]) -> None:
        floor = now - timedelta(days=self.settings.lookback_days)
        period = self.settings.metric_period_seconds
        for res in resources:
            if res.state not in COLLECT_STATES.get(res.type, set()):
                continue
            for metric in METRICS.get(res.type, {}):
                with session_scope() as s:
                    last = s.scalar(
                        select(func.max(MetricHourly.ts)).where(
                            MetricHourly.resource_id == res.id, MetricHourly.metric == metric
                        )
                    )
                start = max(floor, _aware(last) - timedelta(hours=1)) if last else floor
                try:
                    series = self.provider.get_metric(res, metric, start, now, period)
                except Exception as exc:
                    log.warning("metric %s/%s failed: %s", res.id, metric, exc)
                    stats["errors"].append(f"metric {res.id}/{metric}: {exc}")
                    continue
                with session_scope() as s:
                    stats["metric_points"] += upsert_metrics(s, series, now)

    def warm_prices(self, resources: list[Resource], stats: dict[str, Any]) -> None:
        """Fetch prices for current specs and their step-down candidates into the ``prices`` cache."""
        book = PriceBook(self.provider)
        for res in resources:
            try:
                if res.type == "ec2":
                    t = res.spec.get("instance_type")
                    book.ec2_hourly(t, res.region)
                    if (down := catalog.step_down(t, catalog.EC2_TYPES)) is not None:
                        book.ec2_hourly(down, res.region)
                elif res.type == "ebs":
                    for vt in {res.spec.get("volume_type"), "gp3"}:
                        book.ebs_component(vt, "storage", res.region)
                    book.ebs_component("gp3", "iops", res.region)
                    book.ebs_component("gp3", "throughput", res.region)
                elif res.type == "rds":
                    cls, eng = res.spec.get("instance_class"), res.spec.get("engine", "mysql")
                    for c in filter(None, [cls, catalog.step_down(cls, catalog.RDS_TYPES)]):
                        book.rds_hourly(c, eng, bool(res.spec.get("multi_az")), res.region)
                        book.rds_hourly(c, eng, False, res.region)
            except Exception as exc:
                stats["errors"].append(f"price {res.id}: {exc}")
        stats["price_sources"] = book.sources

    def collect_billing(self, now: datetime, stats: dict[str, Any]) -> None:
        if not isinstance(self.provider, AwsProvider):
            return
        with session_scope() as s:
            fresh = s.scalar(
                select(BillingSnapshot).where(
                    BillingSnapshot.scope == "account", BillingSnapshot.fetched_at >= now - timedelta(hours=24)
                )
            )
            if fresh is not None:
                stats["billing"] = "cached"
                return
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_start = (month_start - timedelta(days=1)).replace(day=1)
        try:
            # One call covering the previous full month and month-to-date (cost: $0.01).
            end = now + timedelta(days=1)
            by_service = self.provider.get_cost_by_service(prev_start, end)
        except Exception as exc:
            stats["errors"].append(f"cost explorer: {exc}")
            return
        with session_scope() as s:
            s.add(
                BillingSnapshot(
                    period_start=prev_start,
                    period_end=end,
                    scope="account",
                    by_service=by_service,
                    total=round(sum(by_service.values()), 2),
                    fetched_at=now,
                )  # fmt: skip
            )
        stats["billing"] = "fetched"

    def collect_compute_optimizer(self, now: datetime, stats: dict[str, Any]) -> None:
        if not isinstance(self.provider, AwsProvider):
            return
        try:
            findings = self.provider.get_compute_optimizer_findings()
        except Exception as exc:
            stats["errors"].append(f"compute optimizer: {exc}")
            return
        with session_scope() as s:
            # Replace findings from earlier runs; keep ones other regions wrote during this run.
            s.execute(
                delete(ExternalFinding).where(
                    ExternalFinding.source == "compute_optimizer", ExternalFinding.fetched_at < now
                )
            )
            for f in findings:
                s.add(ExternalFinding(source="compute_optimizer", fetched_at=now, **f))
        stats["compute_optimizer_findings"] = len(findings)


def providers_from_settings(settings: Settings | None = None, profile: str | None = None) -> list[AwsProvider]:
    settings = settings or get_settings()
    profile = profile if profile is not None else settings.aws_profile_readonly
    return [AwsProvider(region, profile=profile) for region in settings.regions]
