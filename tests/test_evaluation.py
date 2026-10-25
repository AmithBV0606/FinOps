import gzip

import numpy as np

from evaluation import sensitivity
from evaluation.azure_traces.run import ec2_type_for, load_real

from .conftest import NOW, make_resource
from .fixtures import ec2_series


def test_vm_size_mapping():
    assert ec2_type_for("2", "4") == "c5.large"
    assert ec2_type_for("4", "16") == "m5.xlarge"
    assert ec2_type_for("8", "64") == "r5.2xlarge"
    assert ec2_type_for(">24", ">64") == "m5.12xlarge"


def test_load_real_trace_format(tmp_path):
    days = 15
    with gzip.open(tmp_path / "vmtable.csv.gz", "wt") as f:
        f.write(f"vmA,s,d,0,{days * 86400},50,2,5,Delay-insensitive,4,8\n")
        f.write("vmShort,s,d,0,3600,50,2,5,Interactive,2,4\n")
    rng = np.random.default_rng(0)
    for n in (1, 2):  # two files, in time order
        with gzip.open(tmp_path / f"vm_cpu_readings-file-{n}-of-2.csv.gz", "wt") as f:
            half = days * 86400 // 2
            for ts in range((n - 1) * half, n * half, 300):
                f.write(f"{ts},vmA,1,{3 + rng.random()},{1 + rng.random()}\n")
    resources, series, now = load_real(tmp_path, sample=10, min_days=14, seed=0)
    assert [r.id for r in resources] == ["vmA"]
    assert resources[0].spec["instance_type"] == "c5.xlarge"
    assert len(series["vmA"]["CPUUtilization"].points) == days * 24


def test_sensitivity_variants(rules):
    resources = [make_resource(f"i-{i}", "ec2", {"instance_type": "m5.2xlarge"}) for i in range(3)]
    series = {r.id: ec2_series(r.id, dict(base=b, noise=0.5)) for r, b in zip(resources, (1, 15, 30), strict=True)}
    rows = {r["variant"]: r for r in sensitivity.run(resources, series, NOW, rules)}
    assert rows["baseline"]["flagged"] == 3
    assert rows["over_cpu -10pp"]["flagged"] == 2  # the 30% box is no longer over-provisioned
    assert rows["over_cpu -10pp"]["flagged_delta"] == -1
