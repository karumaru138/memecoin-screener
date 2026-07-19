"""Watchlist state machine — the heart of the bot.

Every token is a row in SQLite moving through:

    NEW → WATCHING → CANDIDATE → ALERTED
                  ↘ REJECTED
                  ↘ EXPIRED

Rules (from the plan):
  * NEW        — recorded only. No enrichment (sub-30-min tokens waste rate
                 limit). Promoted to WATCHING at `new_min_age_minutes`, or
                 instantly on migration.
  * WATCHING   — enriched on a decaying schedule: fast (<2h) / slow (2–24h) /
                 hourly (24–48h).
  * CANDIDATE  — passed all hard filters at least once; always fast cadence.
  * ALERTED    — score crossed threshold. Keep tracking for follow-ups
                 (LP pulled, top10 spike) and outcome logging.
  * REJECTED   — failed a hard filter. Authority/LP = permanent; concentration
                 / liquidity get one re-check after `candidate_recheck_hours`.
  * EXPIRED    — `expire_hours` old with no candidacy. Dropped from polling.

This scheduling is what keeps only a few dozen tokens actively enriched at once,
holding the whole system inside DexScreener's 60 req/min.
"""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Optional

from core.config import Config
from core.filters import evaluate, is_permanent_reject
from core.models import ScoreBreakdown, TokenSnapshot, TokenState
from core.scoring import score as compute_score
from db.store import Store
from enrich.enricher import Enricher

log = logging.getLogger("core.watchlist")

# alert_sink(snapshot, breakdown, is_followup, reason) -> awaitable
AlertSink = Callable[[TokenSnapshot, ScoreBreakdown, bool, str], Awaitable[None]]


class Watchlist:
    def __init__(self, store: Store, enricher: Enricher, cfg: Config, alert_sink: AlertSink):
        self.store = store
        self.enricher = enricher
        self.cfg = cfg
        self.alert_sink = alert_sink

    # ── ingestion entrypoint ──────────────────────────────────────────
    async def on_event(self, event: dict) -> None:
        """Record a discovery event from PumpPortal / DexScreener."""
        mint = event["mint"]
        migrated = event["type"] == "migration"
        now = time.time()
        # NEW tokens are checked once they reach min age; schedule accordingly.
        # (created_at is used as the launch estimate until we enrich.)
        first_check = now if migrated else now + self.cfg.pacing.new_min_age_minutes * 60
        await self.store.upsert_new_token(
            mint,
            symbol=event.get("symbol"),
            name=event.get("name"),
            launch_ts=event.get("received_ts") if not migrated else None,
            migrated=migrated,
            migrated_ts=now if migrated else None,
            next_check_ts=first_check,
        )

    # ── scheduler entrypoint (called for each due token) ──────────────
    async def process_due(self, limit: int = 25) -> int:
        due = await self.store.due_tokens(limit=limit)
        for row in due:
            try:
                await self._process_token(row)
            except Exception as e:  # noqa: BLE001 — never let one token kill the loop
                log.exception("processing %s failed: %s", row["mint"], e)
                # back off this token so a poison row doesn't spin
                await self.store.update_token(
                    row["mint"], next_check_ts=time.time() + 300
                )
        return len(due)

    async def _process_token(self, row: dict) -> None:
        mint = row["mint"]
        now = time.time()
        state = TokenState(row["state"])
        launch_ts = row["launch_ts"] or row["created_at"]
        age_min = max(0.0, (now - launch_ts) / 60.0)
        migrated = row["migrated_ts"] is not None

        # ── NEW: don't spend API calls until old enough ───────────────
        if state == TokenState.NEW and not migrated:
            if age_min < self.cfg.pacing.new_min_age_minutes:
                await self.store.update_token(
                    mint, next_check_ts=launch_ts + self.cfg.pacing.new_min_age_minutes * 60
                )
                return
            state = TokenState.WATCHING
            await self.store.update_token(mint, state=state.value)

        # ── expiry check ──────────────────────────────────────────────
        if state in (TokenState.WATCHING,) and age_min > self.cfg.pacing.expire_hours * 60:
            await self.store.update_token(mint, state=TokenState.EXPIRED.value)
            log.debug("EXPIRED %s (age %.1fh)", mint, age_min / 60)
            return

        # ── enrich ────────────────────────────────────────────────────
        prior = await self.store.last_snapshot(mint)
        prior_holders = prior["holders"] if prior else None
        prior_age_h = ((now - prior["ts"]) / 3600.0) if prior else None
        # RugCheck once on entering WATCHING and again on candidacy — otherwise
        # reuse the last known values to conserve its rate limit.
        run_rc = state in (TokenState.WATCHING, TokenState.CANDIDATE) or prior is None

        snap = await self.enricher.enrich(
            mint,
            symbol=row["symbol"], name=row["name"],
            launch_ts=launch_ts, migrated=migrated,
            migrated_ts=row["migrated_ts"],
            prior_holders=prior_holders, prior_holders_age_hours=prior_age_h,
            run_rugcheck=run_rc,
        )
        # carry forward RugCheck if we skipped it this cycle
        if not run_rc and prior:
            snap.security.rugcheck_score = prior["rugcheck_score"]
            snap.security.rugcheck_label = prior["rugcheck_label"]

        # ── hard filters ──────────────────────────────────────────────
        verdict = evaluate(snap, self.cfg.hard_filters)

        breakdown: Optional[ScoreBreakdown] = None
        if verdict.passed:
            breakdown = compute_score(snap, self.cfg.scoring)

        snapshot_id = await self.store.insert_snapshot(snap, verdict.passed, breakdown)

        # ── state transition + alerting ───────────────────────────────
        if not verdict.passed:
            if not verdict.decidable:
                # still missing required facts — keep enriching on cadence
                await self._reschedule(mint, state, age_min)
                return
            await self._handle_reject(mint, state, verdict, age_min)
            return

        # passed hard filters → at least CANDIDATE
        prev_alert_score = row["last_alert_score"]
        await self.store.update_token(
            mint,
            state=TokenState.CANDIDATE.value if state != TokenState.ALERTED else TokenState.ALERTED.value,
            last_score=breakdown.total,
            last_check_ts=now,
            pair_address=snap.pair.pair_address,
        )

        await self._maybe_alert(mint, state, snap, breakdown, prev_alert_score, snapshot_id)
        # candidates always ride the fast lane
        await self._reschedule(mint, TokenState.CANDIDATE, age_min, candidate=True)

    # ── alerting logic ────────────────────────────────────────────────
    async def _maybe_alert(
        self, mint: str, prev_state: TokenState, snap: TokenSnapshot,
        breakdown: ScoreBreakdown, prev_alert_score: Optional[float],
        snapshot_id: int,
    ) -> None:
        threshold = self.cfg.scoring.alert_threshold
        now = time.time()

        already_alerted = prev_state == TokenState.ALERTED
        is_followup = False
        reason = ""

        if not already_alerted:
            if breakdown.total < threshold:
                return  # not worth a ping yet
            reason = "threshold"
        else:
            # follow-up only on a material move (up or down)
            if prev_alert_score is None:
                return
            delta = breakdown.total - prev_alert_score
            if abs(delta) < self.cfg.scoring.realert_on_score_change:
                return
            is_followup = True
            reason = f"score {'+' if delta >= 0 else ''}{delta:.0f}"

        # ── rate cap ──────────────────────────────────────────────────
        window_start = now - 3600
        recent = await self.store.alerts_since(window_start, followups=False)
        if not is_followup and recent >= self.cfg.delivery.max_alerts_per_hour:
            log.warning(
                "alert cap hit (%d/h) — suppressing %s (score %.0f). "
                "Consider raising alert_threshold.",
                self.cfg.delivery.max_alerts_per_hour, mint, breakdown.total,
            )
            return

        # ── dispatch ──────────────────────────────────────────────────
        if not self.cfg.runtime.alert_dry_run:
            await self.alert_sink(snap, breakdown, is_followup, reason)

        alert_id = await self.store.insert_alert(
            mint, breakdown.total, is_followup=is_followup, reason=reason,
            price_usd=snap.pair.price_usd, market_cap_usd=snap.pair.market_cap_usd,
            liquidity_usd=snap.pair.liquidity_usd, snapshot_id=snapshot_id,
        )
        await self.store.update_token(
            mint, state=TokenState.ALERTED.value, last_alert_score=breakdown.total
        )
        if not is_followup:
            await self.store.schedule_outcomes(
                alert_id, mint, self.cfg.outcome.checkpoints_hours, base_ts=now
            )
        log.info("ALERT %s score=%.0f (%s%s)", mint, breakdown.total,
                 "follow-up: " if is_followup else "", reason)

    # ── rejection handling ────────────────────────────────────────────
    async def _handle_reject(self, mint: str, state: TokenState, verdict, age_min: float) -> None:
        reason = verdict.primary_reason or "hard filter"
        if is_permanent_reject(verdict):
            await self.store.update_token(
                mint, state=TokenState.REJECTED.value,
                reject_reason=reason, reject_ts=time.time(),
                recheck_after_ts=None,
            )
            log.debug("REJECTED(permanent) %s: %s", mint, reason)
            return
        # soft reject (concentration / liquidity): one re-check later
        row = await self.store.get_token(mint)
        already_rechecked = row and row.get("recheck_after_ts") is not None
        if already_rechecked and (row["recheck_after_ts"] or 0) <= time.time():
            await self.store.update_token(
                mint, state=TokenState.REJECTED.value,
                reject_reason=reason, reject_ts=time.time(),
            )
            log.debug("REJECTED(final) %s: %s", mint, reason)
        else:
            recheck_ts = time.time() + self.cfg.pacing.candidate_recheck_hours * 3600
            await self.store.update_token(
                mint, state=TokenState.WATCHING.value,
                reject_reason=reason, recheck_after_ts=recheck_ts,
                next_check_ts=recheck_ts,
            )
            log.debug("SOFT-REJECT %s: %s (recheck in %.0fh)",
                      mint, reason, self.cfg.pacing.candidate_recheck_hours)

    # ── scheduling ────────────────────────────────────────────────────
    async def _reschedule(self, mint: str, state: TokenState, age_min: float,
                          candidate: bool = False) -> None:
        p = self.cfg.pacing
        if candidate or state == TokenState.CANDIDATE:
            interval = p.fast_check_minutes
        elif age_min < 120:
            interval = p.fast_check_minutes
        elif age_min < 24 * 60:
            interval = p.slow_check_minutes
        else:
            interval = p.hourly_check_minutes
        await self.store.update_token(
            mint, next_check_ts=time.time() + interval * 60, last_check_ts=time.time()
        )
