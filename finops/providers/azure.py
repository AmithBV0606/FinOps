"""Azure provider stub — demonstrates that the interface is provider-neutral (not implemented)."""

from __future__ import annotations

from .base import CloudProvider


class AzureProvider(CloudProvider):
    name = "azure"

    def __init__(self, *args, **kwargs):  # noqa: D401
        pass

    def list_compute(self):  # Microsoft.Compute/virtualMachines
        raise NotImplementedError("Azure support is out of scope; see ARCHITECTURE.md §1")

    def list_block_storage(self):  # Microsoft.Compute/disks
        raise NotImplementedError

    def list_databases(self):  # Microsoft.Sql/servers/databases
        raise NotImplementedError

    def get_metric(self, resource, metric, start, end, period=300):  # Azure Monitor
        raise NotImplementedError

    def get_price(self, sku_descriptor, region):  # Azure Retail Prices API
        raise NotImplementedError

    def describe_action(self, action):
        raise NotImplementedError

    def apply_action(self, action):
        raise NotImplementedError
