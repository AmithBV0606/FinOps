"""SQLAlchemy models (SQLite for the dissertation; the types are Postgres-compatible)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class ResourceRow(Base):
    __tablename__ = "resources"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    account: Mapped[str] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(8), index=True)  # ec2 | ebs | rds
    name: Mapped[str] = mapped_column(String(256), default="")
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MetricHourly(Base):
    __tablename__ = "metrics_hourly"
    __table_args__ = (UniqueConstraint("resource_id", "metric", "ts", name="uq_metric_point"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), index=True)
    metric: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    p50: Mapped[float] = mapped_column(Float)
    p95: Mapped[float] = mapped_column(Float)
    max: Mapped[float] = mapped_column(Float)
    sum: Mapped[float] = mapped_column(Float)
    period_seconds: Mapped[int] = mapped_column(Integer, default=300)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriceRow(Base):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("provider", "region", "sku_descriptor", name="uq_price"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(32))
    sku_descriptor: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32))
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    source: Mapped[str] = mapped_column(String(16), default="api")  # api | static
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), default="analyze")  # collect | analyze | full
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resources_scanned: Mapped[int] = mapped_column(Integer, default=0)
    total_savings: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")


class RecommendationRow(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    classification: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk_tier: Mapped[str] = mapped_column(String(16))
    current_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recommended_spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    action_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    current_monthly_cost: Mapped[float] = mapped_column(Float, default=0.0)
    recommended_monthly_cost: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_savings: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    resource: Mapped[ResourceRow] = relationship()
    actions: Mapped[list[ActionRow]] = relationship(back_populates="recommendation")


class ActionRow(Base):
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendations.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(48))
    # dry_run | approved | executed | failed | rolled_back | blocked
    status: Mapped[str] = mapped_column(String(16), default="dry_run")
    requested_by: Mapped[str] = mapped_column(String(64), default="system")
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plan: Mapped[list[Any]] = mapped_column(JSON, default=list)
    before_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rollback: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[str] = mapped_column(Text, default="")

    recommendation: Mapped[RecommendationRow] = relationship(back_populates="actions")


class ExternalFinding(Base):
    """Findings from AWS Compute Optimizer (validation baseline, §9.2)."""

    __tablename__ = "external_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), default="compute_optimizer")
    resource_id: Mapped[str] = mapped_column(String(128), index=True)
    resource_type: Mapped[str] = mapped_column(String(8))
    finding: Mapped[str] = mapped_column(String(64))
    recommended_spec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    monthly_savings: Mapped[float] = mapped_column(Float, default=0.0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BillingSnapshot(Base):
    """Cached Cost Explorer results (≤ 1 call/day)."""

    __tablename__ = "billing_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str] = mapped_column(String(128), default="account")  # account | tag:key=value
    by_service: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
