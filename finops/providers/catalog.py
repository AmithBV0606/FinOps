"""Instance-type catalog and static fallback prices.

The live system prices everything through the AWS Pricing API (see ``aws.AwsProvider.get_price``)
and learns vCPU/memory from ``DescribeInstanceTypes``. This module provides:

* the size ordering needed for "step down one size in the same family",
* vCPU / memory / burstable baselines for common families, and
* a static us-east-1 Linux on-demand price table (USD) used when the Pricing API is unavailable
  (offline runs, unit tests, the Azure public-trace evaluation). Prices are approximate list
  prices and are labelled ``source=static`` wherever they are used.
"""

from __future__ import annotations

from dataclasses import dataclass

SIZE_ORDER = [
    "nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "3xlarge", "4xlarge",
    "6xlarge", "8xlarge", "9xlarge", "10xlarge", "12xlarge", "16xlarge", "18xlarge", "24xlarge",
    "32xlarge", "48xlarge",
]  # fmt: skip

HOURS_PER_MONTH = 730


@dataclass(frozen=True)
class InstanceType:
    name: str
    vcpu: int
    memory_gib: float
    hourly_usd: float | None = None
    burstable_baseline_pct: float | None = None  # per-vCPU baseline for T-family

    @property
    def family(self) -> str:
        return split_type(self.name)[0]

    @property
    def size(self) -> str:
        return split_type(self.name)[1]


def split_type(name: str) -> tuple[str, str]:
    """``m5.large`` -> (``m5``, ``large``); ``db.r5.xlarge`` -> (``db.r5``, ``xlarge``)."""
    family, _, size = name.rpartition(".")
    return family, size


def size_rank(size: str) -> int:
    return SIZE_ORDER.index(size) if size in SIZE_ORDER else -1


# --- EC2 ---------------------------------------------------------------------------------------
_STANDARD = {  # size: (vcpu multiplier vs large, memory multiplier vs large)
    "large": 1, "xlarge": 2, "2xlarge": 4, "4xlarge": 8, "8xlarge": 16, "12xlarge": 24,
    "16xlarge": 32, "24xlarge": 48,
}  # fmt: skip
_C5_SIZES = {"large": 1, "xlarge": 2, "2xlarge": 4, "4xlarge": 8, "9xlarge": 18, "12xlarge": 24,
             "18xlarge": 36, "24xlarge": 48}  # fmt: skip

# family: (vcpu at large, memory GiB at large, $/h at large, size table)
_FIXED_FAMILIES = {
    "m5": (2, 8, 0.096, _STANDARD), "m6i": (2, 8, 0.096, _STANDARD), "m6a": (2, 8, 0.0864, _STANDARD),
    "m7i": (2, 8, 0.1008, _STANDARD), "m6g": (2, 8, 0.077, _STANDARD), "m7g": (2, 8, 0.0816, _STANDARD),
    "c5": (2, 4, 0.085, _C5_SIZES), "c6i": (2, 4, 0.085, _STANDARD), "c6g": (2, 4, 0.068, _STANDARD),
    "c7g": (2, 4, 0.0725, _STANDARD), "r5": (2, 16, 0.126, _STANDARD), "r6i": (2, 16, 0.126, _STANDARD),
    "r6g": (2, 16, 0.1008, _STANDARD), "r7g": (2, 16, 0.1071, _STANDARD),
}  # fmt: skip

# T-family: size -> (vcpu, mem, per-vCPU baseline %)
_T3_SHAPE = {
    "nano": (2, 0.5, 5), "micro": (2, 1, 10), "small": (2, 2, 20), "medium": (2, 4, 20),
    "large": (2, 8, 30), "xlarge": (4, 16, 40), "2xlarge": (8, 32, 40),
}  # fmt: skip
_T2_SHAPE = {
    "nano": (1, 0.5, 5), "micro": (1, 1, 10), "small": (1, 2, 20), "medium": (2, 4, 20),
    "large": (2, 8, 30), "xlarge": (4, 16, 22.5), "2xlarge": (8, 32, 17),
}  # fmt: skip
_T_PRICES = {
    "t2": [0.0058, 0.0116, 0.023, 0.0464, 0.0928, 0.1856, 0.3712],
    "t3": [0.0052, 0.0104, 0.0208, 0.0416, 0.0832, 0.1664, 0.3328],
    "t3a": [0.0047, 0.0094, 0.0188, 0.0376, 0.0752, 0.1504, 0.3008],
    "t4g": [0.0042, 0.0084, 0.0168, 0.0336, 0.0672, 0.1344, 0.2688],
}
BURSTABLE_FAMILIES = set(_T_PRICES)
GRAVITON_FAMILIES = {"t4g", "m6g", "m7g", "c6g", "c7g", "r6g", "r7g"}

EC2_TYPES: dict[str, InstanceType] = {}
for _fam, (_vcpu, _mem, _price, _sizes) in _FIXED_FAMILIES.items():
    for _size, _mult in _sizes.items():
        _name = f"{_fam}.{_size}"
        EC2_TYPES[_name] = InstanceType(_name, _vcpu * _mult, _mem * _mult, round(_price * _mult, 4))
for _fam, _prices in _T_PRICES.items():
    _shape = _T2_SHAPE if _fam == "t2" else _T3_SHAPE
    for (_size, (_vcpu, _mem, _base)), _price in zip(_shape.items(), _prices, strict=True):
        _name = f"{_fam}.{_size}"
        EC2_TYPES[_name] = InstanceType(_name, _vcpu, _mem, _price, _base)


# --- RDS (MySQL/PostgreSQL, Single-AZ, $/h; Multi-AZ is 2x) ------------------------------------
_RDS_SHAPES = {
    "db.t3": {"micro": (2, 1), "small": (2, 2), "medium": (2, 4), "large": (2, 8), "xlarge": (4, 16),
              "2xlarge": (8, 32)},
    "db.t4g": {"micro": (2, 1), "small": (2, 2), "medium": (2, 4), "large": (2, 8), "xlarge": (4, 16),
               "2xlarge": (8, 32)},
}  # fmt: skip
_RDS_T_PRICES = {
    ("db.t3", "mysql"): [0.017, 0.034, 0.068, 0.136, 0.272, 0.544],
    ("db.t3", "postgres"): [0.018, 0.036, 0.072, 0.145, 0.29, 0.579],
    ("db.t4g", "mysql"): [0.016, 0.032, 0.065, 0.129, 0.258, 0.517],
    ("db.t4g", "postgres"): [0.016, 0.032, 0.065, 0.129, 0.258, 0.517],
}
_RDS_FIXED = {  # family: (vcpu at large, mem at large, {engine: $/h at large})
    "db.m5": (2, 8, {"mysql": 0.171, "postgres": 0.178}),
    "db.m6g": (2, 8, {"mysql": 0.152, "postgres": 0.159}),
    "db.m6i": (2, 8, {"mysql": 0.171, "postgres": 0.178}),
    "db.r5": (2, 16, {"mysql": 0.24, "postgres": 0.25}),
    "db.r6g": (2, 16, {"mysql": 0.215, "postgres": 0.225}),
}

RDS_TYPES: dict[str, InstanceType] = {}
RDS_PRICES: dict[tuple[str, str], float] = {}  # (class, engine) -> $/h single-AZ
for (_fam, _engine), _prices in _RDS_T_PRICES.items():
    for (_size, (_vcpu, _mem)), _price in zip(_RDS_SHAPES[_fam].items(), _prices, strict=True):
        _name = f"{_fam}.{_size}"
        RDS_TYPES[_name] = InstanceType(_name, _vcpu, _mem, None, 20)
        RDS_PRICES[(_name, _engine)] = _price
for _fam, (_vcpu, _mem, _by_engine) in _RDS_FIXED.items():
    for _size, _mult in _STANDARD.items():
        _name = f"{_fam}.{_size}"
        RDS_TYPES[_name] = InstanceType(_name, _vcpu * _mult, _mem * _mult)
        for _engine, _price in _by_engine.items():
            RDS_PRICES[(_name, _engine)] = round(_price * _mult, 4)


# --- EBS ($/GB-month etc.) ---------------------------------------------------------------------
EBS_PRICES = {
    "gp2": {"storage": 0.10},
    "gp3": {"storage": 0.08, "iops": 0.005, "throughput": 0.04},  # iops > 3000, MiB/s > 125
    "io1": {"storage": 0.125, "iops": 0.065},
    "io2": {"storage": 0.125, "iops": 0.065},
    "st1": {"storage": 0.045},
    "sc1": {"storage": 0.015},
    "standard": {"storage": 0.05},
}
SNAPSHOT_GB_MONTH = 0.05
RDS_STORAGE_GB_MONTH = {"gp2": 0.115, "gp3": 0.115, "io1": 0.125, "standard": 0.10}

GP3_BASELINE_IOPS = 3000
GP3_BASELINE_THROUGHPUT = 125


def normalise_engine(engine: str) -> str:
    engine = (engine or "").lower()
    if "postgres" in engine:
        return "postgres"
    if "mariadb" in engine:
        return "mariadb"
    return "mysql" if "mysql" in engine else engine


def register_instance_type(name: str, vcpu: int, memory_gib: float) -> None:
    """Add a type discovered at runtime (``DescribeInstanceTypes``) to the catalog."""
    if name not in EC2_TYPES:
        EC2_TYPES[name] = InstanceType(name, vcpu, memory_gib)


def family_sizes(name: str, catalog: dict[str, InstanceType]) -> list[str]:
    family, _ = split_type(name)
    sizes = [t.size for t in catalog.values() if t.family == family and size_rank(t.size) >= 0]
    return sorted(set(sizes), key=size_rank)


def step_down(name: str, catalog: dict[str, InstanceType], steps: int = 1) -> str | None:
    """Return the instance type ``steps`` sizes smaller in the same family, if it exists."""
    family, size = split_type(name)
    sizes = family_sizes(name, catalog)
    if size not in sizes:
        return None
    idx = sizes.index(size) - steps
    return f"{family}.{sizes[idx]}" if idx >= 0 else None


def static_ec2_price(instance_type: str) -> float | None:
    t = EC2_TYPES.get(instance_type)
    return t.hourly_usd if t else None


def static_rds_price(instance_class: str, engine: str, multi_az: bool = False) -> float | None:
    price = RDS_PRICES.get((instance_class, normalise_engine(engine)))
    if price is None:
        price = RDS_PRICES.get((instance_class, "mysql"))
    if price is None:
        return None
    return price * (2 if multi_az else 1)
