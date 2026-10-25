"""Scale test (§9.5): run the EC2 analyzer over Azure Public Dataset VM traces.

Data: https://github.com/Azure/AzurePublicDataset (V2, 2019). Download the VM table and as many CPU
reading files as you want into ``evaluation/azure_traces/data/`` (git-ignored), e.g.:

    BASE=https://azurepublicdatasettraces.blob.core.windows.net/azurepublicdatasetv2/trace_data
    curl -o data/vmtable.csv.gz $BASE/vmtable/vmtable.csv.gz
    curl -o data/vm_cpu_readings-file-1-of-195.csv.gz \
         $BASE/vm_cpu_readings/vm_cpu_readings-file-1-of-195.csv.gz      # ...up to 195

The 195 reading files cover the 30-day trace in time order, so a 14-day window needs roughly the
first 90 files. Then:

    python -m evaluation.azure_traces.run --data evaluation/azure_traces/data --sample 5000

``--synthetic N`` instead generates N VMs from a mixture of idle/low/diurnal/busy profiles. It
exists only to exercise the pipeline and measure runtime — its classification mix reflects the
chosen mixture, not real-world waste, and must not be reported as a finding.

Mapping assumptions (documented in the report):
* VM size → EC2 type by core bucket (size) and GB-per-core (family: ≤2 c5, ≤4 m5, else r5).
* Azure readings are 5-minute avg CPU per VM — treated like CloudWatch ``CPUUtilization`` Average.
* The traces have no network or memory data, so idle is decided on CPU alone (network = 0) and
  over-provisioning uses the CPU-only path.
"""

from __future__ import annotations

import argparse
import glob
import json
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from evaluation import sensitivity
from finops.analyzer import Context, analyze
from finops.config import get_rules
from finops.providers.aggregate import aggregate_hourly
from finops.providers.base import MetricPoint, MetricSeries, Resource
from finops.savings.engine import aggregate
from finops.savings.pricing import PriceBook

RESULTS = Path(__file__).resolve().parents[1] / "results"
TRACE_START = datetime(2019, 1, 1, tzinfo=UTC)  # V2 timestamps are seconds from trace start
VMTABLE_COLS = ["vmid", "subscriptionid", "deploymentid", "vmcreated", "vmdeleted", "maxcpu", "avgcpu",
                "p95maxcpu", "vmcategory", "cores", "memory"]  # fmt: skip
READING_COLS = ["timestamp", "vmid", "mincpu", "maxcpu", "avgcpu"]
CORE_SIZES = {2: "large", 4: "xlarge", 8: "2xlarge", 12: "4xlarge", 24: "8xlarge", 30: "12xlarge"}


def _bucket(value) -> float:
    s = str(value).strip()
    return float(s.lstrip(">")) + (6 if s.startswith(">") else 0)


def ec2_type_for(cores, memory_gb) -> str:
    c, m = _bucket(cores), _bucket(memory_gb)
    per_core = m / c if c else 4
    family = "c5" if per_core <= 2 else "m5" if per_core <= 4 else "r5"
    size = next((v for k, v in sorted(CORE_SIZES.items()) if c <= k), "12xlarge")
    if family == "c5" and size == "12xlarge":
        size = "9xlarge"
    return f"{family}.{size}"


def vm_resource(vmid: str, itype: str, category: str = "") -> Resource:
    # Tagged as managed non-prod so risk tiers reflect the action type, not missing tags.
    return Resource(id=vmid, provider="azure-trace", account="trace", region="us-east-1", type="ec2",
                    name=category, tags={"env": "test", "finops:managed": "true", "category": category},
                    spec={"instance_type": itype}, state="running")  # fmt: skip


# ------------------------------------------------------------------ real traces
def load_real(data: Path, sample: int, min_days: float, seed: int) -> tuple[list, dict, datetime]:
    vmtable = pd.read_csv(data / "vmtable.csv.gz", header=None, names=VMTABLE_COLS)
    vmtable["lifetime_days"] = (vmtable["vmdeleted"] - vmtable["vmcreated"]) / 86400
    eligible = vmtable[vmtable["lifetime_days"] >= min_days]
    picked = eligible.sample(n=min(sample, len(eligible)), random_state=seed)
    print(f"vmtable: {len(vmtable):,} VMs, {len(eligible):,} live ≥ {min_days} d, sampled {len(picked):,}")
    wanted = set(picked["vmid"])

    raw: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    files = sorted(glob.glob(str(data / "vm_cpu_readings-file-*.csv.gz")),
                   key=lambda p: int(Path(p).name.split("-")[2]))  # fmt: skip
    if not files:
        raise SystemExit(f"no vm_cpu_readings-file-*.csv.gz in {data}")
    last_ts = 0
    for path in files:
        for chunk in pd.read_csv(path, header=None, names=READING_COLS, chunksize=2_000_000):
            chunk = chunk[chunk["vmid"].isin(wanted)]
            last_ts = max(last_ts, int(chunk["timestamp"].max()) if len(chunk) else 0)
            for vmid, ts, cpu in zip(chunk["vmid"], chunk["timestamp"], chunk["avgcpu"], strict=True):
                raw[vmid].append((TRACE_START + timedelta(seconds=int(ts)), float(cpu)))
        print(f"  read {Path(path).name}: {sum(map(len, raw.values())):,} readings kept")

    resources, series = [], {}
    for row in picked.itertuples():
        if row.vmid not in raw:
            continue
        resources.append(vm_resource(row.vmid, ec2_type_for(row.cores, row.memory), str(row.vmcategory)))
        series[row.vmid] = {"CPUUtilization": MetricSeries(row.vmid, "CPUUtilization", aggregate_hourly(raw[row.vmid]))}
    return resources, series, TRACE_START + timedelta(seconds=last_ts)


# ------------------------------------------------------------------ synthetic population
PROFILES = {  # name: (weight, base range, noise, diurnal range)
    "idle": (0.25, (0.3, 3.0), 0.5, (0, 0)),
    "low": (0.35, (4, 20), 3, (0, 10)),
    "diurnal": (0.25, (10, 35), 5, (10, 40)),
    "busy": (0.15, (45, 75), 8, (0, 20)),
}


def load_synthetic(n: int, days: int, seed: int) -> tuple[list, dict, datetime]:
    rng = np.random.default_rng(seed)
    now = datetime(2026, 1, 31, tzinfo=UTC)
    hours = np.arange(days * 24)
    ts = [now - timedelta(hours=int(days * 24 - h)) for h in hours]
    hod = np.array([t.hour for t in ts])
    names, weights = zip(*[(k, v[0]) for k, v in PROFILES.items()], strict=True)
    types = [t for t in ("c5", "m5", "r5") for _ in range(1)]
    resources, series = [], {}
    for i in range(n):
        profile = rng.choice(names, p=weights)
        _, (lo, hi), noise, (dlo, dhi) = PROFILES[profile]
        base, diurnal = rng.uniform(lo, hi), rng.uniform(dlo, dhi)
        p50 = np.clip(base + diurnal * np.clip(np.sin((hod - 6) / 12 * np.pi), 0, None)
                      + rng.normal(0, noise, hours.size), 0, 100)  # fmt: skip
        p95 = np.clip(p50 + np.abs(rng.normal(noise * 1.6, noise / 2, hours.size)), 0, 100)
        mx = np.clip(p95 + np.abs(rng.normal(noise, noise / 2, hours.size)), 0, 100)
        vmid = f"syn-{i:06d}"
        itype = f"{rng.choice(types)}.{rng.choice(['large', 'xlarge', '2xlarge', '4xlarge'])}"
        resources.append(vm_resource(vmid, itype, f"synthetic-{profile}"))
        points = [
            MetricPoint(t, float(a), float(b), float(c), float(a * 12))
            for t, a, b, c in zip(ts, p50, p95, mx, strict=True)
        ]
        series[vmid] = {"CPUUtilization": MetricSeries(vmid, "CPUUtilization", points)}
    return resources, series, now


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", type=Path, help="directory with vmtable.csv.gz and vm_cpu_readings files")
    src.add_argument("--synthetic", type=int, metavar="N", help="generate N synthetic VMs (pipeline test only)")
    ap.add_argument("--sample", type=int, default=5000)
    ap.add_argument("--min-days", type=float, default=14)
    ap.add_argument("--days", type=int, default=30, help="synthetic window length")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-sensitivity", action="store_true")
    args = ap.parse_args()

    t0 = time.perf_counter()
    if args.data:
        resources, series, now = load_real(args.data, args.sample, args.min_days, args.seed)
        tag = "azure_traces"
    else:
        resources, series, now = load_synthetic(args.synthetic, args.days, args.seed)
        tag = "synthetic_population"
    load_s = time.perf_counter() - t0

    rules = get_rules()
    ctx = Context(rules, PriceBook(provider=None, use_db=False), now)
    t1 = time.perf_counter()
    recs = analyze(resources, series, ctx)
    analyze_s = time.perf_counter() - t1
    summary = aggregate(recs)
    summary.update({
        "source": tag, "vms": len(recs), "load_seconds": round(load_s, 2), "analyze_seconds": round(analyze_s, 3),
        "vms_per_second": round(len(recs) / analyze_s, 1) if analyze_s else None,
        "by_target_type": dict(Counter(r.recommended_spec.get("instance_type") for r in recs
                                       if r.classification == "over_provisioned").most_common(10)),
        "confidence_mean": round(float(np.mean([r.confidence for r in recs])) if recs else 0, 3),
        "pricing": "static us-east-1 Linux on-demand list prices",
    })  # fmt: skip
    if tag == "synthetic_population":
        summary["warning"] = "SYNTHETIC population — runtime/pipeline check only, not a waste finding"
    print(json.dumps(summary, indent=2))

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{tag}_summary.json").write_text(json.dumps(summary, indent=2))
    pd.DataFrame([{
        "vmid": r.resource_id, "category": r.tags.get("category"), "instance_type": r.current_spec.get("instance_type"),
        "classification": r.classification, "confidence": r.confidence,
        "recommended": r.recommended_spec.get("instance_type"),
        "p95_cpu": r.rationale.get("p95_cpu"), "monthly_savings_usd": r.monthly_savings,
    } for r in recs]).to_csv(RESULTS / f"{tag}_classifications.csv", index=False)  # fmt: skip

    if not args.no_sensitivity:
        rows = sensitivity.run(resources, series, now, rules)
        print()
        sensitivity.print_table(rows)
        (RESULTS / f"{tag}_sensitivity.json").write_text(json.dumps(rows, indent=2))
    print(f"\nresults written to {RESULTS}/{tag}_*")


if __name__ == "__main__":
    main()
