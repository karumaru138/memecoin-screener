"""Outcome tracker — Phase 4, what separates a toy from a tool.

For every alert, we scheduled price checkpoints at +1h/+6h/+24h (see
Store.schedule_outcomes). This task periodically finds due checkpoints, pulls
the current price/liquidity from DexScreener, and records the multiple vs. the
entry price plus whether it rugged (liquidity collapsed).

After ~100 alerts you have a labeled dataset: join `alerts` + `snapshots` +
`outcomes` and check which parameters actually separated winners from losers,
then re-weight the score in config.yaml. That feedback loop is what makes the
weights *yours* instead of a guess.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from db.store import Store
from enrich.dexscreener import DexScreenerClient

log = logging.getLogger("outcome.tracker")

# liquidity below this fraction of entry liquidity counts as "rugged"
RUG_LIQ_FRACTION = 0.2
RUG_LIQ_FLOOR_USD = 2000.0


class OutcomeTracker:
    def __init__(self, store: Store, dex: DexScreenerClient):
        self.store = store
        self.dex = dex

    async def process_due(self, limit: int = 20) -> int:
        due = await self.store.due_outcomes(limit=limit)
        for o in due:
            try:
                await self._record(o)
            except Exception as e:  # noqa: BLE001
                log.warning("outcome %s failed: %s", o["id"], e)
        return len(due)

    async def _record(self, o: dict) -> None:
        mint = o["mint"]
        entry_price = o.get("entry_price")
        pair = await self.dex.pair_stats(mint)

        price = pair.price_usd if pair else None
        liq = pair.liquidity_usd if pair else None

        multiple: Optional[float] = None
        if price is not None and entry_price:
            multiple = price / entry_price

        rugged: Optional[bool] = None
        if liq is not None:
            # compare against entry liquidity if we can find it
            entry_liq = await self._entry_liquidity(o["alert_id"])
            if entry_liq:
                rugged = liq < max(RUG_LIQ_FLOOR_USD, entry_liq * RUG_LIQ_FRACTION)
            else:
                rugged = liq < RUG_LIQ_FLOOR_USD

        await self.store.record_outcome(
            o["id"], price_usd=price, liquidity_usd=liq,
            multiple=multiple, rugged=rugged,
        )
        log.info("outcome %s +%dh: mult=%s rugged=%s",
                 mint, o["checkpoint_h"],
                 f"{multiple:.2f}x" if multiple else "?", rugged)

    async def _entry_liquidity(self, alert_id: int) -> Optional[float]:
        cur = await self.store.db.execute(
            "SELECT liquidity_usd FROM alerts WHERE id = ?", (alert_id,)
        )
        row = await cur.fetchone()
        return row["liquidity_usd"] if row else None
