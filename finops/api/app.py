"""FastAPI REST layer (§4.7). Run: ``uvicorn finops.api.app:app`` (or ``finops serve``)."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel

from finops import services, validation
from finops.actions.remediator import RemediationError, Remediator
from finops.db import ensure_configured

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_configured()
    scheduler = None
    if os.environ.get("FINOPS_SCHEDULER", "false").lower() == "true":
        from finops.scheduler import start_scheduler

        scheduler = start_scheduler(_guarded_run)
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(title="FinOps right-sizing API", version="0.1.0", lifespan=lifespan)


def _guarded_run(collect: bool = True) -> None:
    if not _run_lock.acquire(blocking=False):
        log.info("run already in progress; skipping")
        return
    try:
        services.full_run(collect=collect)
    except Exception:
        log.exception("scheduled run failed")
    finally:
        _run_lock.release()


class ApproveBody(BaseModel):
    approved_by: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/summary")
def summary() -> dict[str, Any]:
    return services.summary()


@app.get("/resources")
def resources(type: str | None = None, classification: str | None = None) -> list[dict[str, Any]]:
    return services.resources(type, classification)


@app.get("/resources/{resource_id}/metrics")
def resource_metrics(resource_id: str, metric: str = "CPUUtilization", days: int = 30) -> list[dict[str, Any]]:
    return services.metric_series(resource_id, metric, days)


@app.get("/recommendations")
def recommendations(
    risk_tier: str | None = None,
    classification: str | None = None,
    type: str | None = None,
    run_id: int | None = None,
) -> list[dict[str, Any]]:
    return services.recommendations(run_id, risk_tier, classification, type)


@app.post("/recommendations/{rec_id}/plan")
def plan(rec_id: int) -> dict[str, Any]:
    try:
        return Remediator().plan(rec_id, requested_by="api")
    except RemediationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/recommendations/{rec_id}/approve")
def approve(rec_id: int, body: ApproveBody) -> dict[str, Any]:
    try:
        return Remediator().approve(rec_id, body.approved_by)
    except RemediationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/actions/{action_id}/execute")
def execute(action_id: int, dry_run: bool = Query(True), actor: str = "api") -> dict[str, Any]:
    try:
        return Remediator().execute(action_id, dry_run=dry_run, actor=actor)
    except RemediationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/actions/{action_id}/rolled-back")
def rolled_back(action_id: int, actor: str, note: str = "") -> dict[str, Any]:
    try:
        return Remediator().mark_rolled_back(action_id, actor, note)
    except RemediationError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/actions")
def actions(status: str | None = None) -> list[dict[str, Any]]:
    return services.actions(status)


@app.get("/runs")
def runs(limit: int = 50) -> list[dict[str, Any]]:
    return services.runs(limit)


@app.post("/runs", status_code=202)
def trigger_run(background: BackgroundTasks, collect: bool = True) -> dict[str, Any]:
    if _run_lock.locked():
        raise HTTPException(409, "a run is already in progress")
    background.add_task(_guarded_run, collect)
    return {"status": "started", "collect": collect}


@app.get("/validation")
def validation_report() -> dict[str, Any]:
    return {"compute_optimizer": validation.compare_latest(), "realised": validation.realised_savings()}
