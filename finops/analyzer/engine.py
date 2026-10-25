"""Analyzer entry points.

* ``analyze`` is pure: resources + series in, recommendations out (used by tests and the
  public-trace evaluation).
* ``run_analysis`` loads the latest inventory and metrics from the DB, analyzes, and persists a
  ``runs`` row plus one ``recommendations`` row per resource.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from finops.config import Rules, get_rules
from finops.db import MetricHourly, RecommendationRow, ResourceRow, Run, session_scope
from finops.providers.base import CloudProvider, MetricPoint, MetricSeries, Resource
from finops.savings.engine import aggregate
from finops.savings.pricing import PriceBook

from . import rules_ebs, rules_ec2, rules_rds
from .common import Context, Recommendation

RuleFn = Callable[[Resource, dict[str, MetricSeries], Context], Recommendation]
RULES: dict[str, RuleFn] = {"ec2": rules_ec2.evaluate, "ebs": rules_ebs.evaluate, "rds": rules_rds.evaluate}


def analyze(
    resources: Iterable[Resource],
    series: dict[str, dict[str, MetricSeries]],
    ctx: Context,
) -> list[Recommendation]:
    out = []
    for res in resources:
        fn = RULES.get(res.type)
        if fn is not None:
            out.append(fn(res, series.get(res.id, {}), ctx))
    return out


def row_to_resource(row: ResourceRow) -> Resource:
    return Resource(
        id=row.id, provider=row.provider, account=row.account, region=row.region, type=row.type,
        name=row.name, tags=row.tags or {}, spec=row.spec or {}, state=row.state,
        first_seen=row.first_seen, last_seen=row.last_seen,
    )  # fmt: skip


def load_series(session, resource_ids: list[str], since: datetime) -> dict[str, dict[str, MetricSeries]]:
    out: dict[str, dict[str, MetricSeries]] = defaultdict(dict)
    rows = session.execute(
        select(MetricHourly)
        .where(MetricHourly.resource_id.in_(resource_ids), MetricHourly.ts >= since)
        .order_by(MetricHourly.resource_id, MetricHourly.metric, MetricHourly.ts)
    ).scalars()
    for m in rows:
        s = out[m.resource_id].get(m.metric)
        if s is None:
            s = out[m.resource_id][m.metric] = MetricSeries(m.resource_id, m.metric, [], m.period_seconds)
        s.points.append(MetricPoint(m.ts, m.p50, m.p95, m.max, m.sum))
    return out


def run_analysis(
    provider: CloudProvider | None = None,
    rules: Rules | None = None,
    now: datetime | None = None,
    stale_after: timedelta = timedelta(days=2),
) -> dict:
    rules = rules or get_rules()
    now = now or datetime.now(UTC)
    ctx = Context(rules=rules, prices=PriceBook(provider), now=now)
    started = time.perf_counter()
    with session_scope() as s:
        rows = s.scalars(select(ResourceRow)).all()
        # Resources not seen by the collector recently are gone (terminated/deleted).
        live = [
            row for row in rows
            if (row.last_seen if row.last_seen.tzinfo else row.last_seen.replace(tzinfo=UTC)) >= now - stale_after
        ]  # fmt: skip
        resources = [row_to_resource(row) for row in live]
        series = load_series(s, [r.id for r in resources], now - timedelta(days=rules.window.preferred_days))
    # Analyze outside the write transaction: the PriceBook caches prices in its own sessions.
    recs = analyze(resources, series, ctx)
    with session_scope() as s:
        run = Run(kind="analyze", started_at=now)
        s.add(run)
        s.flush()
        for rec in recs:
            s.add(
                RecommendationRow(
                    resource_id=rec.resource_id,
                    run_id=run.id,
                    classification=rec.classification,
                    confidence=rec.confidence,
                    risk_tier=rec.risk_tier,
                    current_spec=rec.current_spec,
                    recommended_spec=rec.recommended_spec,
                    action_type=rec.action_type,
                    current_monthly_cost=rec.current_monthly_cost,
                    recommended_monthly_cost=rec.recommended_monthly_cost,
                    monthly_savings=rec.monthly_savings,
                    rationale={**rec.rationale, "price_sources": dict(ctx.prices.sources)},
                    created_at=now,
                )  # fmt: skip
            )
        summary = aggregate(recs)
        run.resources_scanned = len(recs)
        run.total_savings = summary["total_monthly_savings"]
        run.status = "ok"
        run.finished_at = datetime.now(UTC)
        run.notes = f"analysis took {time.perf_counter() - started:.2f}s; prices {ctx.prices.sources}"
        return {"run_id": run.id, **summary}
