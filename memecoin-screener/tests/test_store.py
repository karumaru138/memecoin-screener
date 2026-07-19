"""Integration-ish tests for the SQLite store + watchlist state transitions.

Uses an in-memory-ish temp DB and fake enricher/alert-sink so no network is
touched. Verifies: NEW->WATCHING promotion, a clean token reaching ALERTED,
outcome scheduling, and a hard-fail reaching REJECTED.
"""
from __future__ import annotations

import time

import pytest

from core.config import Config
from core.models import TokenSnapshot, TokenState
from core.watchlist import Watchlist
from db.store import Store
from tests.test_filters import clean_snapshot


class FakeEnricher:
    """Returns a preset snapshot for a mint, ignoring the network entirely."""
    def __init__(self, snap_factory):
        self.snap_factory = snap_factory

    async def enrich(self, mint, **kwargs):
        snap: TokenSnapshot = self.snap_factory(mint)
        snap.mint = mint
        return snap


@pytest.fixture
async def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_new_token_promotes_and_alerts(store):
    cfg = Config()
    sent = []

    async def sink(snap, breakdown, is_followup, reason):
        sent.append((snap.mint, breakdown.total, is_followup, reason))

    def factory(mint):
        # a strong, clean, 2h-old token that should alert
        snap = clean_snapshot()
        snap.social.has_socials = True
        snap.social.paid_profile = True
        snap.social.active_meta = True
        snap.holder_growth_1h = 300
        return snap

    wl = Watchlist(store, FakeEnricher(factory), cfg, sink)

    # ingest a fresh NEW token
    await wl.on_event({"type": "new", "mint": "MINT_A", "symbol": "A",
                       "name": "Acoin", "received_ts": time.time()})
    tok = await store.get_token("MINT_A")
    assert tok["state"] == TokenState.NEW.value

    # force it due and old enough by backdating created_at/launch
    old = time.time() - 3 * 3600
    await store.update_token("MINT_A", created_at=old, launch_ts=old, next_check_ts=0)

    await wl.process_due()

    tok = await store.get_token("MINT_A")
    assert tok["state"] == TokenState.ALERTED.value
    assert sent and sent[0][0] == "MINT_A"

    # outcomes were scheduled at the configured checkpoints
    due = await store.db.execute("SELECT COUNT(*) c FROM outcomes")
    row = await due.fetchone()
    assert row["c"] == len(cfg.outcome.checkpoints_hours)


@pytest.mark.asyncio
async def test_hard_fail_rejects(store):
    cfg = Config()

    async def sink(*a):
        raise AssertionError("should not alert a rejected token")

    def factory(mint):
        snap = clean_snapshot()
        snap.security.mint_revoked = False   # permanent kill switch
        return snap

    wl = Watchlist(store, FakeEnricher(factory), cfg, sink)
    old = time.time() - 3 * 3600
    await store.upsert_new_token("MINT_B", symbol="B", name=None, launch_ts=old,
                                 migrated=False, migrated_ts=None, next_check_ts=0)
    await store.update_token("MINT_B", state="WATCHING", created_at=old, launch_ts=old)

    await wl.process_due()
    tok = await store.get_token("MINT_B")
    assert tok["state"] == TokenState.REJECTED.value
