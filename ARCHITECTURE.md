# FinOps Dashboard & Automated Right-Sizing — Architecture & Implementation Plan

**Project:** FinOps dashboard and automated right-sizing for cloud resources (AWS)
**Context:** BITS WILP dissertation. Evaluated on a real AWS account (friend's) in read-only mode, with automated actions restricted to tagged, non-production resources.
**Status:** Plan — ready for implementation.

---

## 1. Problem statement

Cloud accounts routinely carry 25–35% waste: VMs sized for peak that idle at 3% CPU, unattached disks, dev databases on production-class instances. Native tools (AWS Compute Optimizer, Cost Explorer) are recommendation-only, per-provider, and rarely acted on.

This project builds a **decision engine**: utilization + inventory + pricing in → *idle / over-provisioned* flags, concrete right-sizing recommendations, a priced monthly savings figure, and a safe (dry-run, approval-gated) remediation path out. The dashboard is the human-facing view of those decisions.

**Out of scope:** containers/EKS, Lambda, Reserved Instance / Savings Plan purchase planning, multi-account orgs, Azure/GCP implementations (provider interface is designed for them, only AWS is implemented).

---

## 2. Goals & success criteria

| # | Goal | Measured by |
|---|------|-------------|
| G1 | Collect 14–30 days of utilization for EC2, EBS, RDS | Metrics stored for 100% of in-scope resources |
| G2 | Classify each resource as `idle`, `over_provisioned`, `right_sized`, `insufficient_data` | Rules documented; unit-tested against synthetic series |
| G3 | Produce a concrete recommendation with ₹/$ monthly savings | Every non-`right_sized` resource has a priced recommendation |
| G4 | Safe automated remediation | Dry-run default, tag allow-list, approval gate, full audit log, rollback notes |
| G5 | Quantify savings | (a) predicted monthly savings, (b) agreement % with Compute Optimizer, (c) realised savings on next bill for applied actions |
| G6 | Evaluation at scale | Analyzer run on Azure Public Dataset VM traces (breadth) in addition to live account (depth) |

---

## 3. High-level architecture

```
                ┌──────────────────────────────────────────────────────────┐
                │                      AWS account                         │
                │  EC2 / EBS / RDS  ·  CloudWatch  ·  Pricing API          │
                │  Cost Explorer    ·  Compute Optimizer (validation)      │
                └───────────────┬──────────────────────────┬───────────────┘
                   read-only role│                          │ action role (tag-scoped)
                                 ▼                          ▲
┌───────────────┐  ┌───────────────────────┐   ┌────────────┴────────────┐
│  Scheduler    │─▶│  Collector            │   │  Remediator             │
│  (cron/APSch) │  │  inventory + metrics  │   │  dry-run → approve →    │
└───────────────┘  │  + prices → DB        │   │  execute → audit log    │
                   └───────────┬───────────┘   └────────────▲────────────┘
                               ▼                            │
                   ┌───────────────────────┐   ┌────────────┴────────────┐
                   │  SQLite / Postgres    │◀─▶│  Analyzer + Savings     │
                   │  resources, metrics,  │   │  rules engine, pricing, │
                   │  recommendations,     │   │  confidence, risk tier  │
                   │  actions              │   └─────────────────────────┘
                   └───────────┬───────────┘
                               ▼
                   ┌───────────────────────┐
                   │  FastAPI (REST)       │
                   └───────────┬───────────┘
                               ▼
                   ┌───────────────────────┐
                   │  Dashboard (Streamlit)│
                   └───────────────────────┘
```

Everything except the AWS resources runs **locally on a laptop** (zero hosting cost). Later it can be containerised and run on a small instance if desired.

---

## 4. Components

### 4.1 Provider abstraction (`finops/providers/`)
- `base.py` — `CloudProvider` interface: `list_compute()`, `list_block_storage()`, `list_databases()`, `get_metric(resource, metric, start, end, period)`, `get_price(sku_descriptor)`, `apply_action(action)`.
- `aws.py` — boto3 implementation. Azure/GCP stubs with `NotImplementedError` to show extensibility.
- All downstream code depends only on the interface + normalised dataclasses (`Resource`, `MetricSeries`, `PriceQuote`).

### 4.2 Collector (`finops/collector/`)
Runs daily (configurable). Steps:
1. **Inventory**: `describe_instances`, `describe_volumes`, `describe_db_instances`, tags, attachment state, instance class, storage type/size, Multi-AZ flag.
2. **Metrics** (CloudWatch, 5-min basic metrics, aggregated to 1-hour p50/p95/max, lookback 30 d):
   - EC2: `CPUUtilization`, `NetworkIn`, `NetworkOut`, `EBSReadOps/WriteOps`; `mem_used_percent` if CloudWatch agent present (optional).
   - EBS: `VolumeReadOps`, `VolumeWriteOps`, `VolumeIdleTime`, attachment status.
   - RDS: `CPUUtilization`, `DatabaseConnections`, `FreeableMemory`, `ReadIOPS`, `WriteIOPS`, `FreeStorageSpace`.
3. **Pricing**: AWS Pricing API (`GetProducts`) for on-demand hourly price of current and candidate instance classes, EBS per-GB-month, RDS per-hour. Cached locally (prices change rarely).
4. **Billing context** (optional, 1 call/day): Cost Explorer `GetCostAndUsage` grouped by service for the "actual spend" tiles. Keep calls minimal ($0.01/request).
5. Persist to DB with `collected_at` timestamp (append-only metrics, upsert inventory).

### 4.3 Storage (`finops/db/`)
SQLite via SQLAlchemy for the dissertation (single file, zero ops). Schema:

| Table | Key columns |
|-------|-------------|
| `resources` | id, provider, account, region, type (ec2/ebs/rds), name, tags(json), spec(json: class, vcpu, mem, size_gb, multi_az…), state, first_seen, last_seen |
| `metrics_hourly` | resource_id, metric, ts, p50, p95, max, sum |
| `prices` | provider, region, sku_descriptor, unit, price, currency, fetched_at |
| `recommendations` | id, resource_id, run_id, classification, confidence, risk_tier, current_spec, recommended_spec, action_type, current_monthly_cost, recommended_monthly_cost, monthly_savings, rationale(json), created_at |
| `actions` | id, recommendation_id, status (dry_run/approved/executed/failed/rolled_back), requested_by, approved_by, executed_at, before_state(json), after_state(json), log |
| `runs` | id, started_at, finished_at, resources_scanned, total_savings |

### 4.4 Analyzer — rules engine (`finops/analyzer/`)
Each rule is a pure function `Resource + MetricSeries[] → Recommendation | None`, making them unit-testable. Thresholds live in `config/rules.yaml`.

**Window:** last 14 days minimum, 30 preferred. Fewer than 7 days → `insufficient_data`.

| Resource | Classification | Rule (defaults) | Recommendation |
|----------|---------------|-----------------|----------------|
| EC2 | `idle` | p95 CPU < 5% AND avg network < 5 MB/day | stop (then terminate after review) |
| EC2 | `over_provisioned` | p95 CPU < 40% (AND p95 mem < 50% if available) | step down one size in same family; if burstable candidate fits, suggest t-family |
| EC2 | `right_sized` | otherwise | — |
| EBS | `idle` | state = `available` (unattached) for > 7 d | snapshot → delete |
| EBS | `idle` | attached but read+write ops ≈ 0 for 14 d | review / detach |
| EBS | `over_provisioned` | gp2 / io1 with low IOPS | migrate to gp3 (cheaper, same perf) |
| RDS | `idle` | p95 connections = 0 for 14 d | stop (7-day limit) / snapshot & delete |
| RDS | `over_provisioned` | p95 CPU < 30% AND p95 connections low AND freeable memory > 50% | step down instance class |
| RDS | `config` | Multi-AZ on resources tagged env=dev/test | disable Multi-AZ |

**Confidence score (0–1):** function of window length, metric coverage, and variance (stable low usage → high confidence; spiky → lower).
**Risk tier:** `safe_auto` (unattached EBS, gp2→gp3, stop idle dev EC2) · `approval` (resize EC2/RDS: requires restart) · `manual_only` (anything tagged `env=prod` or no tags).

Optional stretch: a simple forecasting check (e.g. 7-day rolling max vs. recommended size capacity headroom) so a downsized instance still covers observed peaks + 20%.

### 4.5 Savings engine (`finops/savings/`)
- `monthly_cost(spec) = hourly_price × 730` for compute; `gb × price_per_gb_month` for storage.
- `monthly_savings = current − recommended` (0 for `right_sized`).
- Aggregate per run: total, by service, by risk tier, by tag (team/env).
- Currency: store USD; display INR with a configurable rate.

### 4.6 Remediator (`finops/actions/`)
- **Default mode: dry-run.** Prints exactly which API call would be made, with before/after.
- **Allow-list:** acts only on resources tagged `finops:managed=true` AND not `env=prod`. Enforced in code *and* in the IAM policy (`aws:ResourceTag` condition).
- **Approval gate:** recommendation → "Approve" in dashboard (or CLI flag) → execution. `safe_auto` tier can optionally auto-execute if `AUTO_APPLY=true`.
- **Actions implemented:** `stop_instance`, `modify_instance_type` (stop → modify → start), `create_snapshot` + `delete_volume`, `modify_volume` (gp2→gp3), `modify_db_instance` (class), `stop_db_instance`.
- **Audit:** every attempt logged to `actions` with full before/after state; snapshot IDs recorded for rollback.

### 4.7 API (`finops/api/`) — FastAPI
`GET /summary` · `GET /resources?type=&classification=` · `GET /recommendations?risk_tier=` · `POST /recommendations/{id}/approve` · `POST /actions/{id}/execute?dry_run=true` · `GET /runs` · `POST /runs` (trigger collection + analysis).

### 4.8 Dashboard (`dashboard/`) — Streamlit
Pages:
1. **Overview** — total monthly spend (Cost Explorer), total identified savings, savings by service / risk tier, waste %.
2. **Resources** — table with classification, p95 CPU sparkline, current vs recommended, savings; filters by type/tag/tier.
3. **Recommendations & Actions** — approve / dry-run / execute buttons, action log.
4. **Validation** — side-by-side with Compute Optimizer findings; predicted vs realised savings over time.

### 4.9 Scheduler
`APScheduler` inside the API process (or plain cron): daily collect → analyze → persist. Manual trigger from dashboard.

---

## 5. Security & account hygiene (do this on day 1)

1. **Never use root or admin keys.** Create IAM role/user `finops-readonly` with: `CloudWatchReadOnlyAccess`, `AmazonEC2ReadOnlyAccess`, `AmazonRDSReadOnlyAccess`, `ce:GetCostAndUsage`, `pricing:GetProducts`, `compute-optimizer:Get*`.
2. Separate `finops-remediator` role with write actions **conditioned on** `aws:ResourceTag/finops:managed = true` and `aws:ResourceTag/env != prod`. Assume it only when executing.
3. Credentials via AWS profile / env vars; `.env` in `.gitignore`; no keys in code or report.
4. AWS Budget alerts at $5 and $20 (the project itself costs ≈ $0; this is a safety net).
5. Written consent from the account owner to use utilization/billing data in an academic report; anonymise account IDs, names, and absolute spend in the dissertation.

---

## 6. Repository layout

```
FinOps/
├── ARCHITECTURE.md
├── README.md
├── pyproject.toml
├── config/
│   ├── rules.yaml            # thresholds, windows
│   └── settings.example.env
├── finops/
│   ├── providers/  (base.py, aws.py, azure.py[stub], gcp.py[stub])
│   ├── collector/
│   ├── db/         (models.py, session.py, migrations/)
│   ├── analyzer/   (rules_ec2.py, rules_ebs.py, rules_rds.py, confidence.py)
│   ├── savings/
│   ├── actions/
│   ├── api/
│   └── cli.py      # `finops collect | analyze | recommend | apply --dry-run`
├── dashboard/
│   └── app.py
├── iam/
│   ├── readonly-policy.json
│   └── remediator-policy.json
├── evaluation/
│   ├── azure_traces/         # public dataset loader + notebook
│   └── compare_compute_optimizer.py
├── tests/
│   ├── fixtures/             # synthetic metric series
│   └── test_rules_*.py
└── docs/                     # dissertation figures, outline, report
```

---

## 7. Tech stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.11+ |
| Cloud SDK | boto3 |
| Data | pandas, numpy |
| DB | SQLite + SQLAlchemy (Postgres-compatible models) |
| API | FastAPI + uvicorn |
| UI | Streamlit + Plotly |
| Scheduling | APScheduler |
| Config | pydantic-settings, YAML |
| Tests | pytest, moto (AWS mocking) |
| Tooling | ruff, mypy, pre-commit, GitHub Actions |

---

## 8. Implementation phases

| Phase | Weeks | Deliverable | Done when |
|-------|-------|-------------|-----------|
| 0. Setup | 1 | Repo, IAM roles, budget alerts, consent, `settings.env` | `aws sts get-caller-identity` works with read-only role |
| 1. Collector | 2–3 | Inventory + metrics + prices into SQLite; CLI `finops collect` | 7 days of data accumulating for all EC2/EBS/RDS |
| 2. Analyzer + Savings | 4–6 | Rules engine, confidence, risk tier, priced recommendations; `finops recommend` | Unit tests pass on synthetic series; recommendations table populated |
| 3. API + Dashboard | 7–9 | FastAPI + Streamlit pages 1–3 | Can browse, filter, and dry-run from UI |
| 4. Remediator | 10–11 | Tag-scoped actions, approval flow, audit log, rollback notes | One real `safe_auto` action executed on a tagged non-prod resource |
| 5. Evaluation | 12–13 | Compute Optimizer comparison; Azure trace run; realised-savings tracking | Validation page populated; results tables for the report |
| 6. Report | 14–16 | Dissertation + demo video | Submitted |

**Start collecting metrics in week 1–2 even before the analyzer exists** — the 30-day window is the long pole.

---

## 9. Evaluation design (for the dissertation)

1. **Coverage:** % of in-scope resources with ≥14 days of metrics.
2. **Agreement:** precision/recall of our `idle`/`over_provisioned` flags vs. AWS Compute Optimizer findings on the same resources.
3. **Predicted savings:** total ₹/month, by service and tier.
4. **Realised savings:** for applied actions, compare next billing period vs. baseline (Cost Explorer, tag-filtered).
5. **Scale test:** run analyzer over Azure Public Dataset VM traces (thousands of VMs) → distribution of classifications, runtime, sensitivity to thresholds.
6. **Threshold sensitivity:** vary CPU/network thresholds ±10 pp, report change in flagged count and savings.

---

## 10. Risks & mitigations

| Risk | Mitigation |
|------|-----------|
| Real account has little waste → thin results | Azure/Google public traces for breadth; threshold-sensitivity analysis; optionally a tiny tagged sandbox instance |
| No memory metrics (no CloudWatch agent) | Rules degrade gracefully to CPU+network; document limitation |
| Accidental action on prod | Tag allow-list in code + IAM condition; dry-run default; approval gate |
| Cost Explorer API charges | ≤ 1 call/day, cached |
| Credentials leak | profiles/env only, pre-commit secret scan |

---

## 11. Immediate next steps (implementation kickoff)

1. `git init`, create layout from §6, `pyproject.toml` with deps from §7.
2. Write `iam/readonly-policy.json`, create role in the AWS account, configure local profile `finops-ro`.
3. Implement `providers/base.py` + `providers/aws.py` inventory calls; verify against the live account.
4. Implement CloudWatch metric pull + SQLite persistence; schedule daily.
5. Write `config/rules.yaml` and first EC2 rule with tests (moto / synthetic fixtures).
