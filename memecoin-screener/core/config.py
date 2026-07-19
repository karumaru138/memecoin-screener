"""Config + secrets loading.

`config.yaml` holds all tunable thresholds and weights (safe to commit).
`.env` holds secrets (gitignored). We validate the shape of config.yaml with
pydantic so a typo fails loudly at startup rather than silently mis-scoring.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


# ── config.yaml schema ────────────────────────────────────────────────

class HardFilters(BaseModel):
    require_mint_revoked: bool = True
    require_freeze_revoked: bool = True
    require_lp_secured: bool = True
    lp_min_lock_days: int = 30
    max_top10_pct: float = 25
    min_liquidity_usd: float = 20000
    min_age_minutes: float = 30
    max_age_hours: float = 48
    rugcheck_hard_reject_score: Optional[float] = None


class SecurityScoring(BaseModel):
    dev_pct_soft: float = 3.0
    dev_pct_hard: float = 5.0
    dev_penalty_soft: float = 15
    dev_penalty_hard: float = 30
    top10_penalty_per_pct_over: float = 1.0
    metadata_mutable_penalty: float = 5
    bundled_sniper_penalty: float = 15
    rugcheck_max_penalty: float = 30


class TractionScoring(BaseModel):
    liquidity_bands: list[list[float]] = Field(
        default_factory=lambda: [[20000, 10], [50000, 20], [150000, 25]]
    )
    mcap_sweet_low: float = 50000
    mcap_sweet_high: float = 1000000
    mcap_points: float = 25
    volliq_organic_low: float = 1.0
    volliq_organic_high: float = 10.0
    volliq_points: float = 20
    volliq_wash_ratio: float = 20.0
    buys_sells_min: float = 1.2
    buys_sells_points: float = 15
    holder_growth_points: float = 20


class SocialScoring(BaseModel):
    live_socials_points: float = 30
    paid_profile_points: float = 25
    active_meta_points: float = 25
    smart_wallet_points: float = 20


class MomentumScoring(BaseModel):
    price_1h_points: float = 50
    holder_accel_points: float = 50
    vertical_cap: float = 80


class Scoring(BaseModel):
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "security": 0.35, "traction": 0.40, "social": 0.15, "momentum": 0.10
        }
    )
    alert_threshold: float = 70
    realert_on_score_change: float = 15
    security: SecurityScoring = Field(default_factory=SecurityScoring)
    traction: TractionScoring = Field(default_factory=TractionScoring)
    social: SocialScoring = Field(default_factory=SocialScoring)
    momentum: MomentumScoring = Field(default_factory=MomentumScoring)


class Pacing(BaseModel):
    fast_check_minutes: float = 5
    slow_check_minutes: float = 15
    hourly_check_minutes: float = 60
    new_min_age_minutes: float = 30
    candidate_recheck_hours: float = 6
    expire_hours: float = 48
    dexscreener_budget_rpm: float = 45
    helius_rps: float = 8


class TelegramCfg(BaseModel):
    enabled: bool = True


class DiscordCfg(BaseModel):
    enabled: bool = True


class Delivery(BaseModel):
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    discord: DiscordCfg = Field(default_factory=DiscordCfg)
    max_alerts_per_hour: int = 6
    links: dict[str, str] = Field(
        default_factory=lambda: {
            "dexscreener": "https://dexscreener.com/solana/{mint}",
            "rugcheck": "https://rugcheck.xyz/tokens/{mint}",
            "solscan": "https://solscan.io/token/{mint}",
        }
    )


class OutcomeCfg(BaseModel):
    checkpoints_hours: list[int] = Field(default_factory=lambda: [1, 6, 24])


class Monitoring(BaseModel):
    heartbeat_silent_minutes: float = 15
    daily_summary_hour_local: int = 9


class Runtime(BaseModel):
    db_path: str = "screener.db"
    log_path: str = "logs/screener.jsonl"
    alert_dry_run: bool = False


class Config(BaseModel):
    hard_filters: HardFilters = Field(default_factory=HardFilters)
    scoring: Scoring = Field(default_factory=Scoring)
    pacing: Pacing = Field(default_factory=Pacing)
    delivery: Delivery = Field(default_factory=Delivery)
    outcome: OutcomeCfg = Field(default_factory=OutcomeCfg)
    monitoring: Monitoring = Field(default_factory=Monitoring)
    runtime: Runtime = Field(default_factory=Runtime)


# ── secrets from .env / environment ───────────────────────────────────

class Secrets(BaseModel):
    pumpportal_api_key: Optional[str] = None
    helius_rpc_url: Optional[str] = None
    rugcheck_api_key: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    discord_webhook_url: Optional[str] = None


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (avoids a python-dotenv dependency)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # don't clobber a value already exported in the real environment
        os.environ.setdefault(key, val)


def load_secrets(env_path: str | Path = ".env") -> Secrets:
    _load_dotenv(Path(env_path))
    return Secrets(
        pumpportal_api_key=os.environ.get("PUMPPORTAL_API_KEY") or None,
        helius_rpc_url=os.environ.get("HELIUS_RPC_URL") or None,
        rugcheck_api_key=os.environ.get("RUGCHECK_API_KEY") or None,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL") or None,
    )


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = Config(**raw)
    _validate_weights(cfg)
    return cfg


def _validate_weights(cfg: Config) -> None:
    total = sum(cfg.scoring.weights.values())
    if abs(total - 1.0) > 0.001:
        raise ValueError(
            f"scoring.weights must sum to 1.0, got {total:.3f} "
            f"({cfg.scoring.weights})"
        )
    required = {"security", "traction", "social", "momentum"}
    missing = required - set(cfg.scoring.weights)
    if missing:
        raise ValueError(f"scoring.weights missing keys: {missing}")
