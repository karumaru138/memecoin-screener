"""Pydantic models shared across the screener.

These are deliberately permissive: real-world API responses drop fields, send
nulls, and change types. Enrichment code fills in what it can; `None` means
"unknown", which the filters and scorer treat conservatively.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TokenState(str, Enum):
    NEW = "NEW"
    WATCHING = "WATCHING"
    CANDIDATE = "CANDIDATE"
    ALERTED = "ALERTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class SecurityInfo(BaseModel):
    """On-chain / audit facts. Prefer Helius (chain truth) over aggregators."""
    mint_revoked: Optional[bool] = None       # mint authority == None
    freeze_revoked: Optional[bool] = None      # freeze authority == None
    lp_secured: Optional[bool] = None          # LP burned or locked
    lp_lock_days: Optional[float] = None
    top10_pct: Optional[float] = None          # % supply in top-10 non-LP wallets
    dev_pct: Optional[float] = None            # % held by deployer wallet
    metadata_mutable: Optional[bool] = None
    bundled_snipers: Optional[int] = None      # count of same-block insider buys
    rugcheck_score: Optional[float] = None     # rugcheck aggregate (higher = riskier)
    rugcheck_label: Optional[str] = None       # e.g. "Good", "Warning", "Danger"


class PairStats(BaseModel):
    """DexScreener market facts for the token's primary pair."""
    pair_address: Optional[str] = None
    price_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    market_cap_usd: Optional[float] = None
    fdv_usd: Optional[float] = None
    vol24h_usd: Optional[float] = None
    buys_1h: Optional[int] = None
    sells_1h: Optional[int] = None
    price_change_1h: Optional[float] = None    # percent, e.g. 38.0 for +38%
    holders: Optional[int] = None

    @property
    def vol_liq_ratio(self) -> Optional[float]:
        if self.vol24h_usd is None or not self.liquidity_usd:
            return None
        return self.vol24h_usd / self.liquidity_usd

    @property
    def buys_sells_ratio(self) -> Optional[float]:
        if self.buys_1h is None or self.sells_1h is None:
            return None
        if self.sells_1h == 0:
            return float(self.buys_1h) if self.buys_1h else 1.0
        return self.buys_1h / self.sells_1h


class SocialInfo(BaseModel):
    has_socials: Optional[bool] = None         # X / Telegram / website present
    paid_profile: Optional[bool] = None        # DexScreener paid profile / boost
    active_meta: Optional[bool] = None         # rides a currently-trending meta
    smart_wallet_buys: Optional[int] = None    # Phase 3


class TokenSnapshot(BaseModel):
    """Everything the filters + scorer need about a token at one instant."""
    mint: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    ts: float = Field(default_factory=time.time)

    launch_ts: Optional[float] = None          # best estimate of launch time
    migrated: bool = False                     # graduated off pump.fun
    migrated_ts: Optional[float] = None

    security: SecurityInfo = Field(default_factory=SecurityInfo)
    pair: PairStats = Field(default_factory=PairStats)
    social: SocialInfo = Field(default_factory=SocialInfo)

    # holder growth per hour, computed by the watchlist from prior snapshots
    holder_growth_1h: Optional[float] = None
    # holder acceleration (change in growth rate), Phase 3-ish momentum input
    holder_accel: Optional[float] = None

    @property
    def age_minutes(self) -> Optional[float]:
        if self.launch_ts is None:
            return None
        return max(0.0, (self.ts - self.launch_ts) / 60.0)

    @property
    def age_hours(self) -> Optional[float]:
        m = self.age_minutes
        return None if m is None else m / 60.0


class FilterResult(BaseModel):
    passed: bool
    # list of (filter_name, human_reason) for every filter that FAILED
    failures: list[tuple[str, str]] = Field(default_factory=list)

    @property
    def primary_reason(self) -> Optional[str]:
        return self.failures[0][1] if self.failures else None


class ScoreBreakdown(BaseModel):
    security: float = 0.0
    traction: float = 0.0
    social: float = 0.0
    momentum: float = 0.0
    total: float = 0.0
    notes: list[str] = Field(default_factory=list)   # human-readable "why"
