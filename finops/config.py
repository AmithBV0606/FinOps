"""Runtime settings (env / settings.env) and analyzer rule thresholds (rules.yaml)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = ROOT / "config" / "rules.yaml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINOPS_", env_file=str(ROOT / "config" / "settings.env"), extra="ignore"
    )

    database_url: str = f"sqlite:///{ROOT / 'finops.db'}"
    aws_profile_readonly: str | None = "finops-ro"
    aws_profile_remediator: str | None = "finops-rw"
    regions: list[str] = ["ap-south-1"]
    lookback_days: int = 30
    metric_period_seconds: int = 300
    usd_to_inr: float = 88.0
    auto_apply: bool = False
    cost_explorer_enabled: bool = True
    compute_optimizer_enabled: bool = True
    schedule_cron_hour: int = 2
    api_url: str = "http://127.0.0.1:8000"
    rules_path: Path = DEFAULT_RULES_PATH


class WindowRules(BaseModel):
    min_days: float = 7
    preferred_days: float = 30
    rule_days: float = 14


class Ec2Idle(BaseModel):
    p95_cpu_below: float = 5.0
    network_mb_per_day_below: float = 5.0


class Ec2Over(BaseModel):
    p95_cpu_below: float = 40.0
    p95_mem_below: float = 50.0


class Ec2Headroom(BaseModel):
    max_projected_p95_cpu: float = 66.0
    max_projected_peak_cpu: float = 90.0


class Ec2Rules(BaseModel):
    idle: Ec2Idle = Ec2Idle()
    over_provisioned: Ec2Over = Ec2Over()
    headroom: Ec2Headroom = Ec2Headroom()
    suggest_burstable: bool = True


class EbsRules(BaseModel):
    unattached_days: float = 7
    zero_ops_days: float = 14
    zero_ops_per_day_below: float = 1.0
    migrate_to_gp3: list[str] = ["gp2", "io1"]
    io1_max_p95_iops_for_gp3: float = 16000


class RdsIdle(BaseModel):
    max_connections: float = 0


class RdsOver(BaseModel):
    p95_cpu_below: float = 30.0
    p95_connections_below: float = 20
    min_freeable_memory_ratio: float = 0.5


class RdsHeadroom(BaseModel):
    max_projected_p95_cpu: float = 60.0


class RdsRules(BaseModel):
    idle: RdsIdle = RdsIdle()
    over_provisioned: RdsOver = RdsOver()
    headroom: RdsHeadroom = RdsHeadroom()
    multi_az_non_prod_envs: list[str] = ["dev", "test", "staging", "sandbox"]


class RiskRules(BaseModel):
    prod_tag_key: str = "env"
    prod_tag_values: list[str] = ["prod", "production"]
    non_prod_envs: list[str] = ["dev", "test", "staging", "sandbox", "qa"]
    managed_tag_key: str = "finops:managed"
    managed_tag_value: str = "true"


class ConfidenceRules(BaseModel):
    weights: dict[str, float] = {"window": 0.4, "coverage": 0.3, "stability": 0.3}


class Rules(BaseModel):
    window: WindowRules = WindowRules()
    ec2: Ec2Rules = Ec2Rules()
    ebs: EbsRules = EbsRules()
    rds: RdsRules = RdsRules()
    risk: RiskRules = RiskRules()
    confidence: ConfidenceRules = ConfidenceRules()

    @classmethod
    def load(cls, path: Path | str | None = None) -> Rules:
        path = Path(path) if path else DEFAULT_RULES_PATH
        data: dict[str, Any] = yaml.safe_load(path.read_text()) if path.exists() else {}
        return cls.model_validate(data or {})


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_rules() -> Rules:
    return Rules.load(get_settings().rules_path)
