# FinOps dashboard & automated right-sizing (AWS)

Decision engine for EC2, EBS and RDS: utilisation + inventory + pricing in → `idle` /
`over_provisioned` / `config` / `right_sized` / `insufficient_data`, a concrete priced
recommendation, and a dry-run, approval-gated remediation path. Design: [ARCHITECTURE.md](ARCHITECTURE.md).

```
finops/
  providers/   CloudProvider interface, AWS (boto3), Azure/GCP stubs, instance catalog + static prices
  collector/   inventory + CloudWatch metrics (hourly p50/p95/max/sum) + prices + Cost Explorer + Compute Optimizer
  db/          SQLAlchemy models (SQLite by default)
  analyzer/    rules_ec2 / rules_ebs / rules_rds, confidence, risk tier, engine
  savings/     PriceBook (DB cache → Pricing API → static) and cost arithmetic
  actions/     guards (tag allow-list) + Remediator (dry-run → approve → execute → audit)
  api/         FastAPI          dashboard/  Streamlit (Overview, Resources, Actions, Validation)
  cli.py       finops collect | analyze | recommend | plan | approve | apply | validate | serve | demo
iam/           read-only + tag-scoped remediator policies, setup.sh (roles + budget alerts)
evaluation/    Compute Optimizer agreement, Azure trace scale test, threshold sensitivity, realised savings
config/        rules.yaml (all thresholds), settings.example.env
```

## Quick start (offline, no AWS)

```bash
uv venv && uv pip install -e ".[dev]"     # or: python -m venv .venv && pip install -e ".[dev]"
source .venv/bin/activate
pytest -q                                  # 52 tests: rules on synthetic series, moto-mocked AWS, API
finops demo                                # seeds a SYNTHETIC account (ids demo-*) and analyzes it
finops recommend
finops serve &                             # API on :8000
streamlit run dashboard/app.py             # dashboard on :8501
```

## Live account (Phase 0 → 1)

1. **Account owner** reviews and runs `iam/setup.sh` (creates `finops-readonly` and
   `finops-remediator` roles, $5/$20 budget alerts) and adds the printed `finops-ro` / `finops-rw`
   profiles to `~/.aws/config`. Never use root or admin keys with this tool.
2. Get written consent to use utilisation/billing data in the report; anonymise ids and spend.
3. `cp config/settings.example.env config/settings.env` and set `FINOPS_REGIONS`.
4. `finops whoami` → must show the `finops-readonly` role.
5. Optional: opt in to Compute Optimizer and activate the `finops:managed` cost-allocation tag.
6. Start collecting **now** — the 30-day window is the long pole:
   `finops collect` daily (cron, or `finops serve --scheduler` which runs at `FINOPS_SCHEDULE_CRON_HOUR` UTC).
7. After ≥ 7 days: `finops analyze && finops recommend`.

## Remediation safety

An action executes only if **all** hold: resource tagged `finops:managed=true`, not `env=prod`
(enforced in code *and* in the IAM policy), action automatable and tier ≠ `manual_only`, the
action is approved (or `safe_auto` with `FINOPS_AUTO_APPLY=true`), the live resource still matches
the analysed spec, and `--no-dry-run` / "Execute for real" was given explicitly. Every step is
logged in `actions` with before/after state, snapshot ids and a rollback command.

```bash
finops plan 12                       # exact API calls, no changes
finops approve 12 --actor alice
finops apply 7                       # dry-run (default)
finops apply 7 --no-dry-run          # uses the finops-rw profile
```

## Evaluation (§9)

```bash
python -m evaluation.compare_compute_optimizer            # precision/recall/agreement vs Compute Optimizer
python -m evaluation.sensitivity                           # ±10 pp thresholds on live data
python -m evaluation.azure_traces.run --data evaluation/azure_traces/data --sample 5000   # real traces
python -m evaluation.azure_traces.run --synthetic 2000     # pipeline/runtime check only — not a finding
python -m evaluation.realised_savings --months 3           # tagged spend before vs after actions
```

Results land in `evaluation/results/` (git-ignored).

## Known limitations

- Static fallback prices are us-east-1 Linux list prices; live runs use the Pricing API per region.
- No memory metrics without the CloudWatch agent → EC2 rules use CPU + network only (flagged in rationale).
- Unattached-EBS age is approximated by creation time on first sighting (AWS does not expose detach time).
- Azure traces have no network/memory data → idle is CPU-only there.
