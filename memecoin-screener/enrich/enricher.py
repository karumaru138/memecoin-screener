"""Enrichment orchestrator.

Given a mint (and whatever we already know about it), assemble a full
`TokenSnapshot` by combining:

  * DexScreener pair stats  (price, liquidity, mcap, vol, buys/sells, socials)
  * Helius RPC              (mint/freeze authority, supply, top-10 concentration)
  * RugCheck report         (aggregate risk, LP lock status)
  * DexScreener feed        (paid profile / boost, active-meta fit)

`lp_secured` is the trickiest fact. pump.fun graduations auto-burn LP, so a
migrated token is treated as LP-secured. Otherwise we trust RugCheck's LP-lock
signal. The rules mirror the plan's "trust the chain, use aggregators for what's
expensive to compute" principle.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from core.models import SecurityInfo, SocialInfo, TokenSnapshot
from enrich.dexscreener import DexScreenerClient
from enrich.helius import HeliusClient
from enrich.rugcheck import RugCheckClient
from ingest.dexscreener_poll import DexScreenerFeed

log = logging.getLogger("enrich.enricher")


class Enricher:
    def __init__(
        self,
        dex: DexScreenerClient,
        helius: HeliusClient,
        rugcheck: RugCheckClient,
        feed: Optional[DexScreenerFeed] = None,
    ):
        self.dex = dex
        self.helius = helius
        self.rugcheck = rugcheck
        self.feed = feed

    async def enrich(
        self,
        mint: str,
        *,
        symbol: Optional[str] = None,
        name: Optional[str] = None,
        launch_ts: Optional[float] = None,
        migrated: bool = False,
        migrated_ts: Optional[float] = None,
        prior_holders: Optional[int] = None,
        prior_holders_age_hours: Optional[float] = None,
        run_rugcheck: bool = True,
    ) -> TokenSnapshot:
        snap = TokenSnapshot(
            mint=mint, symbol=symbol, name=name, ts=time.time(),
            launch_ts=launch_ts, migrated=migrated, migrated_ts=migrated_ts,
        )

        # ── DexScreener market data ───────────────────────────────────
        pair = await self.dex.pair_stats(mint)
        if pair is not None:
            snap.pair = pair
            # backfill launch time from pair creation if we don't have it
            created_ms = getattr(pair, "_pair_created_ms", None)
            if launch_ts is None and created_ms:
                snap.launch_ts = created_ms / 1000.0
            snap.social.has_socials = bool(getattr(pair, "_has_socials", False))

        # ── Helius chain facts ────────────────────────────────────────
        try:
            info = await self.helius.get_mint_info(mint)
        except Exception as e:  # noqa: BLE001
            log.warning("Helius mint info failed for %s: %s", mint, e)
            info = None

        sec = SecurityInfo()
        if info is not None:
            sec.mint_revoked = info.mint_revoked
            sec.freeze_revoked = info.freeze_revoked
            try:
                exclude = {snap.pair.pair_address} if snap.pair.pair_address else None
                sec.top10_pct = await self.helius.top10_pct(
                    mint, total_supply=info.supply, exclude=exclude
                )
            except Exception as e:  # noqa: BLE001
                log.warning("Helius top10 failed for %s: %s", mint, e)

        # ── RugCheck aggregate ────────────────────────────────────────
        if run_rugcheck and self.rugcheck.enabled:
            try:
                rc = await self.rugcheck.report(mint)
            except Exception as e:  # noqa: BLE001
                log.warning("RugCheck failed for %s: %s", mint, e)
                rc = None
            if rc is not None:
                sec.rugcheck_score = rc.score
                sec.rugcheck_label = rc.label
                if rc.lp_locked is not None:
                    sec.lp_secured = rc.lp_locked
                    sec.lp_lock_days = None  # locked; RugCheck doesn't give days cheaply

        # pump.fun graduation auto-burns LP -> treat as secured
        if migrated:
            sec.lp_secured = True
            sec.lp_lock_days = float("inf")

        snap.security = sec

        # ── social context from the shared feed ───────────────────────
        if self.feed is not None:
            snap.social.paid_profile = self.feed.is_boosted(mint) or self.feed.has_profile(mint)
            snap.social.active_meta = self.feed.matches_active_meta(name, symbol)

        # ── holder growth (derivative) if we have a prior reading ─────
        if (
            prior_holders is not None
            and snap.pair.holders is not None
            and prior_holders_age_hours
            and prior_holders_age_hours > 0
        ):
            snap.holder_growth_1h = (snap.pair.holders - prior_holders) / prior_holders_age_hours

        return snap
