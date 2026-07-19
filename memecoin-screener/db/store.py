"""Async SQLite persistence layer (aiosqlite).

Thin data-access wrapper — no business logic (that lives in watchlist.py). All
bot state lives here so a crash-restart loses nothing but a few minutes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from core.models import ScoreBreakdown, TokenSnapshot, TokenState

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store not opened"
        return self._db

    # ── tokens ────────────────────────────────────────────────────────
    async def upsert_new_token(
        self, mint: str, *, symbol: Optional[str], name: Optional[str],
        launch_ts: Optional[float], migrated: bool, migrated_ts: Optional[float],
        next_check_ts: float,
    ) -> bool:
        """Insert a token if unseen. Returns True if it was newly inserted.

        If it already exists and a migration arrives, promote it (record
        migrated_ts and bring the next check forward)."""
        now = time.time()
        cur = await self.db.execute("SELECT mint, state FROM tokens WHERE mint = ?", (mint,))
        row = await cur.fetchone()
        if row is None:
            state = TokenState.WATCHING.value if migrated else TokenState.NEW.value
            await self.db.execute(
                """INSERT INTO tokens
                   (mint, symbol, name, state, created_at, launch_ts,
                    migrated_ts, next_check_ts, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (mint, symbol, name, state, now, launch_ts, migrated_ts,
                 next_check_ts, now),
            )
            await self.db.commit()
            return True

        if migrated:
            # migration promotes an existing token immediately
            await self.db.execute(
                """UPDATE tokens
                   SET migrated_ts = COALESCE(migrated_ts, ?),
                       state = CASE WHEN state IN ('NEW') THEN 'WATCHING' ELSE state END,
                       next_check_ts = MIN(next_check_ts, ?),
                       updated_at = ?
                   WHERE mint = ?""",
                (migrated_ts or now, now, now, mint),
            )
            await self.db.commit()
        return False

    async def due_tokens(self, now: Optional[float] = None, limit: int = 50) -> list[dict]:
        now = now or time.time()
        cur = await self.db.execute(
            """SELECT * FROM tokens
               WHERE state IN ('NEW','WATCHING','CANDIDATE','ALERTED')
                 AND next_check_ts <= ?
               ORDER BY next_check_ts ASC
               LIMIT ?""",
            (now, limit),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_token(self, mint: str) -> Optional[dict]:
        cur = await self.db.execute("SELECT * FROM tokens WHERE mint = ?", (mint,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_token(self, mint: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.db.execute(
            f"UPDATE tokens SET {cols} WHERE mint = ?",
            (*fields.values(), mint),
        )
        await self.db.commit()

    async def count_by_state(self) -> dict[str, int]:
        cur = await self.db.execute("SELECT state, COUNT(*) c FROM tokens GROUP BY state")
        return {r["state"]: r["c"] for r in await cur.fetchall()}

    # ── snapshots ─────────────────────────────────────────────────────
    async def insert_snapshot(
        self, snap: TokenSnapshot, passed_hard: bool,
        breakdown: Optional[ScoreBreakdown],
    ) -> int:
        sec, pair, soc = snap.security, snap.pair, snap.social
        cur = await self.db.execute(
            """INSERT INTO snapshots (
                mint, ts, mint_revoked, freeze_revoked, lp_secured, lp_lock_days,
                top10_pct, dev_pct, metadata_mutable, bundled_snipers,
                rugcheck_score, rugcheck_label, price_usd, liquidity_usd,
                market_cap_usd, fdv_usd, vol24h_usd, vol_liq_ratio, buys_1h,
                sells_1h, buys_sells_ratio, holders, holder_growth_1h,
                price_change_1h, age_minutes, has_socials, paid_profile,
                active_meta, passed_hard, score, score_security, score_traction,
                score_social, score_momentum, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snap.mint, snap.ts, _b(sec.mint_revoked), _b(sec.freeze_revoked),
                _b(sec.lp_secured), _fin(sec.lp_lock_days), sec.top10_pct, sec.dev_pct,
                _b(sec.metadata_mutable), sec.bundled_snipers, sec.rugcheck_score,
                sec.rugcheck_label, pair.price_usd, pair.liquidity_usd,
                pair.market_cap_usd, pair.fdv_usd, pair.vol24h_usd, pair.vol_liq_ratio,
                pair.buys_1h, pair.sells_1h, pair.buys_sells_ratio, pair.holders,
                snap.holder_growth_1h, pair.price_change_1h, snap.age_minutes,
                _b(soc.has_socials), _b(soc.paid_profile), _b(soc.active_meta),
                _b(passed_hard),
                breakdown.total if breakdown else None,
                breakdown.security if breakdown else None,
                breakdown.traction if breakdown else None,
                breakdown.social if breakdown else None,
                breakdown.momentum if breakdown else None,
                json.dumps(snap.model_dump(mode="json")),
            ),
        )
        await self.db.commit()
        return cur.lastrowid

    async def last_snapshot(self, mint: str) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT * FROM snapshots WHERE mint = ? ORDER BY ts DESC LIMIT 1", (mint,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ── alerts ────────────────────────────────────────────────────────
    async def insert_alert(
        self, mint: str, score: float, *, is_followup: bool, reason: str,
        price_usd: Optional[float], market_cap_usd: Optional[float],
        liquidity_usd: Optional[float], snapshot_id: Optional[int],
    ) -> int:
        cur = await self.db.execute(
            """INSERT INTO alerts
               (mint, ts, score, is_followup, reason, price_usd, market_cap_usd,
                liquidity_usd, snapshot_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (mint, time.time(), score, int(is_followup), reason, price_usd,
             market_cap_usd, liquidity_usd, snapshot_id),
        )
        await self.db.commit()
        return cur.lastrowid

    async def alerts_since(self, since_ts: float, followups: bool = False) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM alerts WHERE ts >= ? AND is_followup = ?",
            (since_ts, int(followups)),
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    # ── outcomes ──────────────────────────────────────────────────────
    async def schedule_outcomes(self, alert_id: int, mint: str,
                                checkpoints_hours: list[int], base_ts: float) -> None:
        for h in checkpoints_hours:
            await self.db.execute(
                """INSERT OR IGNORE INTO outcomes
                   (alert_id, mint, checkpoint_h, due_ts) VALUES (?,?,?,?)""",
                (alert_id, mint, h, base_ts + h * 3600),
            )
        await self.db.commit()

    async def due_outcomes(self, now: Optional[float] = None, limit: int = 50) -> list[dict]:
        now = now or time.time()
        cur = await self.db.execute(
            """SELECT o.*, a.price_usd AS entry_price
               FROM outcomes o JOIN alerts a ON a.id = o.alert_id
               WHERE o.recorded_ts IS NULL AND o.due_ts <= ?
               ORDER BY o.due_ts ASC LIMIT ?""",
            (now, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def record_outcome(self, outcome_id: int, *, price_usd: Optional[float],
                             liquidity_usd: Optional[float], multiple: Optional[float],
                             rugged: Optional[bool]) -> None:
        await self.db.execute(
            """UPDATE outcomes SET recorded_ts = ?, price_usd = ?, liquidity_usd = ?,
               multiple = ?, rugged = ? WHERE id = ?""",
            (time.time(), price_usd, liquidity_usd, multiple, _b(rugged), outcome_id),
        )
        await self.db.commit()

    # ── kv ────────────────────────────────────────────────────────────
    async def kv_get(self, key: str) -> Optional[str]:
        cur = await self.db.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    # ── daily-summary helper ──────────────────────────────────────────
    async def summary_counts(self, since_ts: float) -> dict[str, int]:
        out: dict[str, int] = {}
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM tokens WHERE created_at >= ?", (since_ts,))
        out["seen"] = (await cur.fetchone())["c"]
        cur = await self.db.execute(
            "SELECT COUNT(DISTINCT mint) c FROM snapshots WHERE ts >= ? AND passed_hard = 1",
            (since_ts,))
        out["passed_filters"] = (await cur.fetchone())["c"]
        cur = await self.db.execute(
            "SELECT COUNT(*) c FROM alerts WHERE ts >= ? AND is_followup = 0", (since_ts,))
        out["alerted"] = (await cur.fetchone())["c"]
        return out


def _b(x: Optional[bool]) -> Optional[int]:
    return None if x is None else int(bool(x))


def _fin(x: Optional[float]) -> Optional[float]:
    """SQLite can't store inf; store None for 'locked forever/burned'."""
    if x is None:
        return None
    if x == float("inf") or x != x:  # inf or nan
        return None
    return x
