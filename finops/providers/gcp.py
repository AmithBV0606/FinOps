"""GCP provider stub — demonstrates that the interface is provider-neutral (not implemented)."""

from __future__ import annotations

from .base import CloudProvider


class GcpProvider(CloudProvider):
    name = "gcp"

    def __init__(self, *args, **kwargs):  # noqa: D401
        pass

    def list_compute(self):  # compute.instances
        raise NotImplementedError("GCP support is out of scope; see ARCHITECTURE.md §1")

    def list_block_storage(self):  # compute.disks
        raise NotImplementedError

    def list_databases(self):  # sqladmin.instances
        raise NotImplementedError

    def get_metric(self, resource, metric, start, end, period=300):  # Cloud Monitoring
        raise NotImplementedError

    def get_price(self, sku_descriptor, region):  # Cloud Billing Catalog API
        raise NotImplementedError

    def describe_action(self, action):
        raise NotImplementedError

    def apply_action(self, action):
        raise NotImplementedError
