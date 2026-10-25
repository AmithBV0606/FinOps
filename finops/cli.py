"""Command line: ``finops collect | analyze | recommend | plan | approve | apply | runs | serve | demo``."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from finops import services
from finops.config import get_settings
from finops.db import configure


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_whoami(args) -> None:
    from finops.collector import providers_from_settings

    for p in providers_from_settings(profile=args.profile):
        ident = p.client("sts").get_caller_identity()
        _print({"region": p.region, "account": ident["Account"], "arn": ident["Arn"]})


def cmd_collect(args) -> None:
    from finops.collector import Collector, providers_from_settings

    for p in providers_from_settings(profile=args.profile):
        stats = Collector(p).collect(with_billing=not args.no_billing, with_compute_optimizer=not args.no_co)
        _print({"region": p.region, **stats})


def cmd_analyze(args) -> None:
    from finops.analyzer import run_analysis
    from finops.collector import providers_from_settings

    provider = None if args.offline_prices else (providers_from_settings(profile=args.profile) or [None])[0]
    _print(run_analysis(provider=provider))


def cmd_recommend(args) -> None:
    recs = services.recommendations(risk_tier=args.risk_tier, classification=args.classification,
                                    resource_type=args.type)  # fmt: skip
    rate = get_settings().usd_to_inr
    if args.json:
        _print(recs)
        return
    total = 0.0
    print(f"{'id':>5}  {'type':4}  {'resource':24} {'class':18} {'tier':11} {'conf':>4}  "
          f"{'action':26} {'$/mo saved':>10}  {'₹/mo':>9}")  # fmt: skip
    for r in recs:
        if r["classification"] == "right_sized" and not args.all:
            continue
        total += r["monthly_savings"]
        print(f"{r['id']:>5}  {r['resource_type']:4}  {r['resource_id'][:24]:24} {r['classification']:18} "
              f"{r['risk_tier']:11} {r['confidence']:>4.2f}  {(r['action_type'] or '-'):26} "
              f"{r['monthly_savings']:>10.2f}  {r['monthly_savings_inr']:>9.0f}")  # fmt: skip
    print(f"\nTotal identified savings: ${total:,.2f}/month (₹{total * rate:,.0f}/month)")


def cmd_plan(args) -> None:
    from finops.actions import Remediator

    _print(Remediator().plan(args.recommendation_id, requested_by=args.actor))


def cmd_approve(args) -> None:
    from finops.actions import Remediator

    _print(Remediator().approve(args.recommendation_id, approved_by=args.actor))


def cmd_apply(args) -> None:
    from finops.actions import Remediator

    rem = Remediator()
    if args.auto:
        _print(rem.auto_apply(dry_run=args.dry_run))
        return
    if args.action_id is None:
        sys.exit("give an action id (see `finops actions`) or --auto")
    _print(rem.execute(args.action_id, dry_run=args.dry_run, actor=args.actor))


def cmd_actions(args) -> None:
    _print(services.actions(args.status))


def cmd_runs(args) -> None:
    _print(services.runs(args.limit))


def cmd_summary(args) -> None:
    _print(services.summary())


def cmd_validate(args) -> None:
    from finops import validation

    report = validation.compare_latest()
    if not args.rows:
        report.pop("rows", None)
    _print(report)


def cmd_serve(args) -> None:
    import os

    import uvicorn

    if args.scheduler:
        os.environ["FINOPS_SCHEDULER"] = "true"
    uvicorn.run("finops.api.app:app", host=args.host, port=args.port)


def cmd_demo(args) -> None:
    from finops.demo import seed_demo

    _print(seed_demo())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="finops", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--db", help="database URL (overrides FINOPS_DATABASE_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("whoami", help="check AWS credentials (sts get-caller-identity)")
    s.add_argument("--profile")
    s.set_defaults(fn=cmd_whoami)

    s = sub.add_parser("collect", help="inventory + metrics + prices into the DB")
    s.add_argument("--profile", help="AWS profile (default FINOPS_AWS_PROFILE_READONLY)")
    s.add_argument("--no-billing", action="store_true", help="skip the Cost Explorer call")
    s.add_argument("--no-co", action="store_true", help="skip Compute Optimizer")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("analyze", help="run the rules engine and store recommendations")
    s.add_argument("--profile")
    s.add_argument("--offline-prices", action="store_true", help="use cached/static prices only")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("recommend", help="list recommendations from the latest run")
    s.add_argument("--risk-tier", choices=["safe_auto", "approval", "manual_only"])
    s.add_argument("--classification")
    s.add_argument("--type", choices=["ec2", "ebs", "rds"])
    s.add_argument("--all", action="store_true", help="include right_sized")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_recommend)

    s = sub.add_parser("plan", help="dry-run plan for a recommendation")
    s.add_argument("recommendation_id", type=int)
    s.add_argument("--actor", default="cli")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("approve", help="approve a recommendation's action")
    s.add_argument("recommendation_id", type=int)
    s.add_argument("--actor", required=True, help="who approves (recorded in the audit log)")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("apply", help="execute an approved action (dry-run unless --no-dry-run)")
    s.add_argument("action_id", type=int, nargs="?")
    s.add_argument("--auto", action="store_true", help="apply all safe_auto recommendations of the latest run")
    s.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    s.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    s.add_argument("--actor", default="cli")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("actions", help="action audit log")
    s.add_argument("--status")
    s.set_defaults(fn=cmd_actions)

    s = sub.add_parser("runs", help="collection/analysis runs")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_runs)

    sub.add_parser("summary", help="savings summary").set_defaults(fn=cmd_summary)

    s = sub.add_parser("validate", help="agreement with Compute Optimizer")
    s.add_argument("--rows", action="store_true")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("serve", help="run the REST API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--scheduler", action="store_true", help="enable the daily collect+analyze job")
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("demo", help="seed the DB with a synthetic account and analyze it").set_defaults(fn=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    configure(args.db)
    args.fn(args)


if __name__ == "__main__":
    main()
