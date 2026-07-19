"""DexScreener background poller.

Polls the low-frequency discovery endpoints once a minute and maintains a
shared, in-memory view the enrichment stage reads from:

  * trending metas       -> which narratives are hot right now (active_meta)
  * latest token profiles -> tokens with socials / Enhanced Token Info
  * latest boosts         -> tokens the team paid to promote (paid_profile)

This costs ~3-4 req/min of the DexScreener budget. It also surfaces trending
tokens as "new"-style events so we don't rely on PumpPortal alone for
discovery.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from enrich.dexscreener import DexScreenerClient

log = logging.getLogger("ingest.dexscreener_poll")


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


class DexScreenerFeed:
    """Shared, continuously-refreshed market context."""

    def __init__(self, client: DexScreenerClient, out_queue: "asyncio.Queue[dict]",
                 poll_seconds: float = 60.0):
        self.client = client
        self.out = out_queue
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()

        self.active_metas: set[str] = set()
        self.boosted_mints: set[str] = set()
        self.profile_mints: set[str] = set()
        self.last_poll_ts: float = 0.0

    def stop(self) -> None:
        self._stop.set()

    # ── lookups used by enrichment ────────────────────────────────────
    def is_boosted(self, mint: str) -> bool:
        return mint in self.boosted_mints

    def has_profile(self, mint: str) -> bool:
        return mint in self.profile_mints

    def matches_active_meta(self, *texts: Optional[str]) -> bool:
        if not self.active_metas:
            return False
        blob = " ".join(_norm(t) for t in texts if t)
        return any(meta and meta in blob for meta in self.active_metas)

    # ── main loop ─────────────────────────────────────────────────────
    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._poll_once()
                self.last_poll_ts = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("DexScreener poll error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
        log.info("DexScreener feed stopped")

    async def _poll_once(self) -> None:
        metas = await self.client.trending_metas()
        self.active_metas = {
            _norm(m.get("name") or m.get("meta") or m.get("keyword") or (m if isinstance(m, str) else ""))
            for m in metas
        } - {""}

        boosts = await self.client.latest_boosts()
        self.boosted_mints = {
            b.get("tokenAddress") or b.get("mint")
            for b in boosts
            if _norm(b.get("chainId")) in ("", "solana") and (b.get("tokenAddress") or b.get("mint"))
        }

        profiles = await self.client.latest_profiles()
        self.profile_mints = {
            p.get("tokenAddress") or p.get("mint")
            for p in profiles
            if _norm(p.get("chainId")) in ("", "solana") and (p.get("tokenAddress") or p.get("mint"))
        }

        # surface boosted Solana tokens as discovery events too
        for mint in list(self.boosted_mints):
            await self.out.put({
                "type": "new",
                "mint": mint,
                "symbol": None,
                "name": None,
                "pair": None,
                "raw": {"source": "dexscreener_boost"},
                "received_ts": time.time(),
            })

        log.debug("metas=%d boosts=%d profiles=%d",
                  len(self.active_metas), len(self.boosted_mints), len(self.profile_mints))
