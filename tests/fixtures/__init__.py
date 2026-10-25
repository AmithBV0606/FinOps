"""Synthetic metric fixtures (built on finops.synthetic)."""

from __future__ import annotations

from finops.synthetic import constant, series

from ..conftest import NOW

GB = 1024**3


def ec2_series(rid: str, cpu: dict, net_bytes_per_5min: float = 5e6, mem: dict | None = None, days: float = 30):
    out = {
        "CPUUtilization": series(rid, "CPUUtilization", days=days, end=NOW, **cpu),
        "NetworkIn": constant(rid, "NetworkIn", net_bytes_per_5min / 2, days, end=NOW),
        "NetworkOut": constant(rid, "NetworkOut", net_bytes_per_5min / 2, days, end=NOW),
    }
    if mem is not None:
        out["mem_used_percent"] = series(rid, "mem_used_percent", days=days, end=NOW, **mem)
    return out


def ebs_series(rid: str, read_ops_5min: float, write_ops_5min: float, days: float = 30):
    return {
        "VolumeReadOps": constant(rid, "VolumeReadOps", read_ops_5min, days, end=NOW),
        "VolumeWriteOps": constant(rid, "VolumeWriteOps", write_ops_5min, days, end=NOW),
    }


def rds_series(rid: str, cpu: float, connections: float, freeable_gib: float, days: float = 30, peak_conn=None):
    s = {
        "CPUUtilization": series(rid, "CPUUtilization", days=days, base=cpu, noise=1, end=NOW),
        "DatabaseConnections": constant(rid, "DatabaseConnections", connections, days, end=NOW),
        "FreeableMemory": constant(rid, "FreeableMemory", freeable_gib * GB, days, end=NOW),
    }
    if peak_conn is not None:
        s["DatabaseConnections"] = series(
            rid,
            "DatabaseConnections",
            days=days,
            base=connections,
            noise=0,
            spike_every_hours=24,
            spike_height=peak_conn,
            clip=None,
            end=NOW,
        )
    return s
