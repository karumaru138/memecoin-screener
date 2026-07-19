"""Unit tests for the hard filters — the ✋ kill switches.

These are pure functions, so testing is just: build a snapshot, assert the
verdict. A "clean" token (passes everything) is the baseline; each test flips
one fact and asserts the expected rejection or 'not decidable' outcome.
"""
from __future__ import annotations

import time

import pytest

from core.config import HardFilters
from core.filters import evaluate, is_permanent_reject
from core.models import PairStats, SecurityInfo, TokenSnapshot


HF = HardFilters()  # defaults from config schema


def clean_snapshot(**overrides) -> TokenSnapshot:
    """A token that passes every hard filter."""
    now = time.time()
    sec = SecurityInfo(
        mint_revoked=True,
        freeze_revoked=True,
        lp_secured=True,
        lp_lock_days=None,        # burned
        top10_pct=14.0,
        dev_pct=1.2,
        metadata_mutable=False,
        rugcheck_score=1000,
    )
    pair = PairStats(
        pair_address="Pair111",
        price_usd=0.001,
        liquidity_usd=84000,
        market_cap_usd=410000,
        vol24h_usd=350000,
        buys_1h=160, sells_1h=100,
        price_change_1h=38.0,
        holders=1240,
    )
    snap = TokenSnapshot(
        mint="So1anaM1ntAddr",
        symbol="EXAMPLE", name="ExampleCoin",
        ts=now, launch_ts=now - 2 * 3600,   # 2h old
        security=sec, pair=pair,
    )
    for k, v in overrides.items():
        setattr(snap, k, v)
    return snap


def test_clean_token_passes():
    v = evaluate(clean_snapshot(), HF)
    assert v.passed
    assert v.decidable
    assert v.failures == []


def test_mint_authority_live_rejects():
    snap = clean_snapshot()
    snap.security.mint_revoked = False
    v = evaluate(snap, HF)
    assert not v.passed
    assert any(name == "mint_authority" for name, _ in v.failures)
    assert is_permanent_reject(v)


def test_freeze_authority_live_rejects():
    snap = clean_snapshot()
    snap.security.freeze_revoked = False
    v = evaluate(snap, HF)
    assert not v.passed
    assert any(name == "freeze_authority" for name, _ in v.failures)
    assert is_permanent_reject(v)


def test_unsecured_lp_rejects_permanently():
    snap = clean_snapshot()
    snap.security.lp_secured = False
    v = evaluate(snap, HF)
    assert not v.passed
    assert is_permanent_reject(v)


def test_short_lp_lock_rejects():
    snap = clean_snapshot()
    snap.security.lp_secured = True
    snap.security.lp_lock_days = 7   # < 30-day minimum
    v = evaluate(snap, HF)
    assert not v.passed
    assert any(name == "lp_lock" for name, _ in v.failures)


def test_high_concentration_rejects_but_not_permanent():
    snap = clean_snapshot()
    snap.security.top10_pct = 42.0
    v = evaluate(snap, HF)
    assert not v.passed
    assert any(name == "top10_concentration" for name, _ in v.failures)
    assert not is_permanent_reject(v)   # whales can distribute -> re-check


def test_low_liquidity_rejects():
    snap = clean_snapshot()
    snap.pair.liquidity_usd = 5000
    v = evaluate(snap, HF)
    assert not v.passed
    assert any(name == "liquidity" for name, _ in v.failures)


def test_too_young_rejects():
    snap = clean_snapshot()
    snap.launch_ts = snap.ts - 10 * 60  # 10 min old
    v = evaluate(snap, HF)
    assert not v.passed
    assert any(name == "min_age" for name, _ in v.failures)


def test_too_old_rejects():
    snap = clean_snapshot()
    snap.launch_ts = snap.ts - 72 * 3600  # 72h old
    v = evaluate(snap, HF)
    assert not v.passed
    assert any(name == "max_age" for name, _ in v.failures)


def test_unknown_required_fact_is_not_decidable():
    snap = clean_snapshot()
    snap.security.mint_revoked = None      # not yet enriched
    v = evaluate(snap, HF)
    assert not v.passed
    assert not v.decidable                 # keep enriching, don't reject
    assert v.failures == []                # no *failure*, just unknown


def test_migrated_token_lp_treated_secured():
    # emulate enricher: migrated tokens get lp_secured=True, lp_lock_days=inf
    snap = clean_snapshot()
    snap.security.lp_secured = True
    snap.security.lp_lock_days = float("inf")
    v = evaluate(snap, HF)
    assert v.passed


def test_rugcheck_hard_reject():
    hf = HardFilters(rugcheck_hard_reject_score=40000)
    snap = clean_snapshot()
    snap.security.rugcheck_score = 50000
    v = evaluate(snap, hf)
    assert not v.passed
    assert any(name == "rugcheck" for name, _ in v.failures)
