import numpy as np
import pytest

from finops.analyzer import confidence
from finops.analyzer.common import Recommendation
from finops.providers import catalog
from finops.providers.aggregate import aggregate_hourly
from finops.savings import aggregate, compute_monthly, ebs_monthly, gp2_equivalent_gp3
from finops.savings.pricing import PriceBook
from finops.validation import compare

from .conftest import NOW


def test_step_down():
    assert catalog.step_down("m5.2xlarge", catalog.EC2_TYPES) == "m5.xlarge"
    assert catalog.step_down("m5.large", catalog.EC2_TYPES) is None
    assert catalog.step_down("c5.12xlarge", catalog.EC2_TYPES) == "c5.9xlarge"
    assert catalog.step_down("t3.micro", catalog.EC2_TYPES) == "t3.nano"
    assert catalog.step_down("db.r5.xlarge", catalog.RDS_TYPES) == "db.r5.large"
    assert catalog.step_down("x9.unknown", catalog.EC2_TYPES) is None


def test_aggregate_hourly_percentiles():
    from datetime import timedelta

    pts = aggregate_hourly((NOW + timedelta(minutes=5 * i), float(i)) for i in range(24))
    assert len(pts) == 2
    assert pts[0].max == 11 and pts[0].sum == sum(range(12))
    assert pts[0].p50 == pytest.approx(5.5)


def test_confidence_rewards_long_stable_windows(rules):
    flat = np.full(720, 3.0)
    spiky = np.abs(np.random.default_rng(0).normal(3, 6, 720))
    long_stable = confidence.score(30, 1.0, flat, rules)
    short = confidence.score(8, 1.0, flat, rules)
    noisy = confidence.score(30, 1.0, spiky, rules)
    gappy = confidence.score(30, 0.5, flat, rules)
    assert long_stable == pytest.approx(1.0)
    assert short < long_stable and noisy < long_stable and gappy < long_stable
    assert 0 <= min(short, noisy, gappy)


def test_monthly_costs():
    prices = PriceBook(use_db=False)
    assert compute_monthly(0.1) == pytest.approx(73.0)
    assert ebs_monthly(prices, "us-east-1", "gp2", 100) == pytest.approx(10.0)
    assert ebs_monthly(prices, "us-east-1", "gp3", 100, 4000, 200) == pytest.approx(8 + 5 + 3)
    assert ebs_monthly(prices, "us-east-1", "io1", 100, 1000) == pytest.approx(12.5 + 65)
    assert gp2_equivalent_gp3(100) == (3000, 125)
    assert gp2_equivalent_gp3(5334) == (16000, 250)


def test_price_book_caches_to_db():
    book = PriceBook(provider=None)
    assert book.ec2_hourly("m5.large", "us-east-1") == 0.096
    assert book.sources["static"] == 1
    book2 = PriceBook(provider=None)
    assert book2.ec2_hourly("m5.large", "us-east-1") == 0.096  # served from prices table


def test_price_book_prefers_provider():
    class FakeProvider:
        name = "aws"

        def get_price(self, sku, region):
            from finops.providers.base import PriceQuote

            return PriceQuote("aws", region, sku, "Hrs", 0.101)

    book = PriceBook(provider=FakeProvider())
    assert book.ec2_hourly("m5.large", "ap-south-1") == 0.101
    assert book.sources["api"] == 1


def test_savings_aggregate():
    recs = [
        Recommendation(
            "a",
            "ec2",
            "idle",
            risk_tier="safe_auto",
            current_monthly_cost=100,
            recommended_monthly_cost=0,
            tags={"env": "dev", "team": "x"},
        ),
        Recommendation(
            "b", "ebs", "over_provisioned", risk_tier="approval", current_monthly_cost=50, recommended_monthly_cost=40
        ),
        Recommendation("c", "rds", "right_sized", current_monthly_cost=50, recommended_monthly_cost=0),
    ]
    agg = aggregate(recs)
    assert agg["total_monthly_savings"] == 110
    assert agg["total_current_monthly"] == 200
    assert agg["waste_pct"] == 55.0
    assert agg["by_service"] == {"ec2": 100, "ebs": 10}
    assert agg["by_env"] == {"dev": 100, "untagged": 10}


def test_compute_optimizer_agreement():
    ours = {
        "i-1": {"classification": "over_provisioned", "recommended_spec": {"instance_type": "m5.large"}},
        "i-2": {"classification": "idle", "recommended_spec": {}},
        "i-3": {"classification": "right_sized", "recommended_spec": {}},
        "i-4": {"classification": "right_sized", "recommended_spec": {}},
        "i-5": {"classification": "idle", "recommended_spec": {}},
    }
    theirs = {
        "i-1": {"finding": "OVER_PROVISIONED", "recommended_spec": "m5.large"},
        "i-2": {"finding": "Idle"},
        "i-3": {"finding": "Overprovisioned"},
        "i-4": {"finding": "OPTIMIZED"},
        "i-9": {"finding": "Optimized"},
    }
    rep = compare(ours, theirs)
    assert rep["compared"] == 4
    assert rep["confusion"] == {"tp": 2, "fp": 0, "fn": 1, "tn": 1}
    assert rep["precision"] == 1.0
    assert rep["recall"] == pytest.approx(2 / 3)
    assert rep["agreement_pct"] == 75.0
    assert rep["instance_type_match_pct"] == 100.0
    assert rep["only_ours"] == 1 and rep["only_compute_optimizer"] == 1
