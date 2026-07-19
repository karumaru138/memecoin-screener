"""Unit tests for the weighted scoring model.

Pure functions again. We assert relative behavior (better token scores higher)
and that the specific mechanics — vertical-pump cap, wash-trade zeroing,
security penalties — actually fire. The absolute numbers are a starting point,
so we avoid over-fitting tests to exact values where the design is meant to be
tuned.
"""
from __future__ import annotations

import time

import pytest

from core.config import Scoring
from core.models import PairStats, SecurityInfo, SocialInfo, TokenSnapshot
from core.scoring import (
    score,
    score_momentum,
    score_security,
    score_social,
    score_traction,
)

CFG = Scoring()


def strong_token() -> TokenSnapshot:
    now = time.time()
    return TokenSnapshot(
        mint="Strong",
        ts=now, launch_ts=now - 2 * 3600,
        security=SecurityInfo(
            mint_revoked=True, freeze_revoked=True, lp_secured=True,
            lp_lock_days=None, top10_pct=12.0, dev_pct=1.0,
            metadata_mutable=False, rugcheck_score=500, rugcheck_label="Good",
        ),
        pair=PairStats(
            liquidity_usd=120000, market_cap_usd=400000, vol24h_usd=480000,
            buys_1h=160, sells_1h=100, price_change_1h=40.0, holders=1200,
        ),
        social=SocialInfo(has_socials=True, paid_profile=True, active_meta=True),
        holder_growth_1h=300, holder_accel=60,
    )


def weak_token() -> TokenSnapshot:
    now = time.time()
    return TokenSnapshot(
        mint="Weak",
        ts=now, launch_ts=now - 2 * 3600,
        security=SecurityInfo(
            mint_revoked=True, freeze_revoked=True, lp_secured=True,
            top10_pct=24.0, dev_pct=6.0, metadata_mutable=True,
            rugcheck_score=30000, rugcheck_label="Warning",
        ),
        pair=PairStats(
            liquidity_usd=21000, market_cap_usd=5_000_000, vol24h_usd=630000,
            buys_1h=90, sells_1h=110, price_change_1h=2.0, holders=300,
        ),
        social=SocialInfo(has_socials=False, paid_profile=False, active_meta=False),
        holder_growth_1h=5,
    )


def test_strong_scores_higher_than_weak():
    assert score(strong_token(), CFG).total > score(weak_token(), CFG).total


def test_strong_crosses_default_threshold():
    assert score(strong_token(), CFG).total >= 70


def test_weak_below_threshold():
    assert score(weak_token(), CFG).total < 70


def test_security_penalties_apply():
    snap = strong_token()
    snap.security.dev_pct = 6.0            # heavy penalty
    snap.security.metadata_mutable = True  # minor penalty
    sec, notes = score_security(snap, CFG)
    assert sec < 100
    assert any("dev holds" in n for n in notes)


def test_wash_trading_gives_no_volliq_points():
    snap = strong_token()
    snap.pair.vol24h_usd = snap.pair.liquidity_usd * 30  # 30x -> washed
    trac, notes = score_traction(snap, CFG)
    assert any("washed" in n for n in notes)


def test_vertical_pump_is_capped():
    snap = strong_token()
    snap.pair.price_change_1h = 500.0       # vertical
    snap.holder_accel = 100
    mom, notes = score_momentum(snap, CFG)
    assert mom <= CFG.momentum.vertical_cap
    assert any("capped" in n for n in notes)


def test_social_components_additive():
    full = strong_token()
    none = strong_token()
    none.social = SocialInfo(has_socials=False, paid_profile=False, active_meta=False)
    assert score_social(full, CFG)[0] > score_social(none, CFG)[0]


def test_score_is_clamped_0_100():
    b = score(strong_token(), CFG)
    for v in (b.security, b.traction, b.social, b.momentum, b.total):
        assert 0 <= v <= 100
