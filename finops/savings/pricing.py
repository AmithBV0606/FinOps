"""PriceBook: on-demand prices with caching.

Lookup order: in-memory cache → ``prices`` table (if fresher than ``max_age``) → provider Pricing API
→ static catalog (us-east-1 list prices). Every quote carries its ``source`` so reports can show
which savings figures are API-priced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from finops.db import PriceRow, session_scope
from finops.providers import catalog
from finops.providers.base import CloudProvider


class PriceBook:
    def __init__(
        self,
        provider: CloudProvider | None = None,
        use_db: bool = True,
        max_age: timedelta = timedelta(days=30),
    ):
        self.provider = provider
        self.use_db = use_db
        self.max_age = max_age
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}
        self.sources: dict[str, int] = {"api": 0, "static": 0, "missing": 0}

    # -------------------------------------------------------------- generic
    def lookup(self, sku: str, region: str) -> tuple[float | None, str]:
        key = (sku, region)
        if key in self._cache:
            return self._cache[key]
        price, source = self._from_db(sku, region)
        if price is None and self.provider is not None:
            quote = self.provider.get_price(sku, region)
            if quote is not None:
                price, source = quote.price, "api"
                if "GiB" in quote.unit and sku.endswith(":throughput"):
                    price = price / 1024  # normalise GiBps-Mo to MiBps-Mo
                self._to_db(sku, region, quote.unit, price, source)
        if price is None:
            price = self._static(sku)
            source = "static" if price is not None else "missing"
            if price is not None:
                self._to_db(sku, region, "static", price, source)
        self.sources[source] = self.sources.get(source, 0) + 1
        self._cache[key] = (price, source)  # type: ignore[assignment]
        return price, source

    def _from_db(self, sku: str, region: str) -> tuple[float | None, str]:
        if not self.use_db:
            return None, ""
        cutoff = datetime.now(UTC) - self.max_age
        with session_scope() as s:
            row = s.scalar(select(PriceRow).where(PriceRow.sku_descriptor == sku, PriceRow.region == region))
            if row is None:
                return None, ""
            fetched = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
            # Static rows never block a later API refresh.
            if row.source == "static" and self.provider is not None:
                return None, ""
            if fetched < cutoff:
                return None, ""
            return row.price, row.source

    def _to_db(self, sku: str, region: str, unit: str, price: float, source: str) -> None:
        if not self.use_db:
            return
        provider = self.provider.name if self.provider else "aws"
        with session_scope() as s:
            row = s.scalar(select(PriceRow).where(PriceRow.sku_descriptor == sku, PriceRow.region == region))
            if row is None:
                row = PriceRow(provider=provider, region=region, sku_descriptor=sku)
                s.add(row)
            row.unit, row.price, row.source, row.fetched_at = unit, price, source, datetime.now(UTC)

    @staticmethod
    def _static(sku: str) -> float | None:
        parts = sku.split(":")
        if parts[0] == "ec2":
            return catalog.static_ec2_price(parts[1])
        if parts[0] == "rds":
            return catalog.static_rds_price(parts[1], parts[2], parts[3] == "multi-az")
        if parts[0] == "ebs":
            return catalog.EBS_PRICES.get(parts[1], {}).get(parts[2])
        if parts[0] == "snapshot":
            return catalog.SNAPSHOT_GB_MONTH
        if parts[0] == "rds-storage":
            return catalog.RDS_STORAGE_GB_MONTH.get(parts[1], catalog.RDS_STORAGE_GB_MONTH["gp2"])
        return None

    # -------------------------------------------------------------- typed helpers
    def ec2_hourly(self, instance_type: str, region: str) -> float | None:
        return self.lookup(f"ec2:{instance_type}:linux", region)[0]

    def rds_hourly(self, instance_class: str, engine: str, multi_az: bool, region: str) -> float | None:
        engine = catalog.normalise_engine(engine)
        return self.lookup(f"rds:{instance_class}:{engine}:{'multi-az' if multi_az else 'single-az'}", region)[0]

    def ebs_component(self, volume_type: str, component: str, region: str) -> float:
        return self.lookup(f"ebs:{volume_type}:{component}", region)[0] or 0.0

    def snapshot_gb_month(self, region: str) -> float:
        return self.lookup("snapshot:standard:storage", region)[0] or catalog.SNAPSHOT_GB_MONTH

    def rds_storage_gb_month(self, storage_type: str, region: str) -> float:
        return self.lookup(f"rds-storage:{storage_type or 'gp2'}:storage", region)[0] or 0.0
