from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.pop("AWS_PROFILE", None)

from finops.analyzer.common import Context  # noqa: E402
from finops.config import Rules  # noqa: E402
from finops.db import configure  # noqa: E402
from finops.providers.base import Resource  # noqa: E402
from finops.savings.pricing import PriceBook  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def db(tmp_path):
    configure(f"sqlite:///{tmp_path / 'test.db'}")
    yield


@pytest.fixture
def rules() -> Rules:
    return Rules.load()


@pytest.fixture
def ctx(rules) -> Context:
    return Context(rules=rules, prices=PriceBook(provider=None, use_db=False), now=NOW)


def make_resource(rid: str, rtype: str, spec: dict, tags: dict | None = None, state: str | None = None) -> Resource:
    default_state = {"ec2": "running", "ebs": "in-use", "rds": "available"}[rtype]
    return Resource(
        id=rid,
        provider="aws",
        account="123456789012",
        region="us-east-1",
        type=rtype,
        name=rid,
        tags={"env": "dev", "finops:managed": "true"} if tags is None else tags,
        spec=spec,
        state=state or default_state,
    )
