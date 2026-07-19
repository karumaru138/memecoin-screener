"""Self-monitoring: heartbeat + daily summary.

  * Heartbeat — if no ingest event has arrived for `heartbeat_silent_minutes`,
    message you ("stream silent — check me"). The PumpPortal stream dropping
    without recovering is the most likely silent failure.
  * Daily summary — once per day at `daily_summary_hour_local`: tokens seen /
    passed filters / alerted, plus how yesterday's alerts performed.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from core.config import Config
from db.store import Store
from deliver.hub import DeliveryHub

log = logging.getLogger("core.monitor")


class Monitor:
    def __init__(self, cfg: Config, store: Store, hub: DeliveryHub,
                 last_event_ts_getter):
        self.cfg = cfg
        self.store = store
        self.hub = hub
        self._last_event_ts_getter = last_event_ts_getter
        self._silent_warned = False

    async def check_heartbeat(self) -> None:
        last = self._last_event_ts_getter()
        if last <= 0:
            return  # nothing ingested yet since boot; don't warn on cold start
        silent_for = time.time() - last
        limit = self.cfg.monitoring.heartbeat_silent_minutes * 60
        if silent_for > limit:
            if not self._silent_warned:
                await self.hub.notify(
                    f"⚠️ *Stream silent* — no events for {silent_for/60:.0f} min. "
                    f"Check the PumpPortal websocket."
                )
                self._silent_warned = True
        else:
            self._silent_warned = False

    async def maybe_daily_summary(self) -> None:
        now = datetime.now()  # local time (server TZ)
        if now.hour != self.cfg.monitoring.daily_summary_hour_local:
            return
        today_key = now.strftime("%Y-%m-%d")
        last_sent = await self.store.kv_get("last_daily_summary")
        if last_sent == today_key:
            return

        since = time.time() - 24 * 3600
        counts = await self.store.summary_counts(since)
        perf = await self._yesterday_performance(since)

        text = (
            f"📊 *Daily summary* ({today_key})\n"
            f"Seen: {counts['seen']} · Passed filters: {counts['passed_filters']} · "
            f"Alerted: {counts['alerted']}\n"
            f"{perf}"
        )
        await self.hub.notify(text)
        await self.store.kv_set("last_daily_summary", today_key)

    async def _yesterday_performance(self, since: float) -> str:
        cur = await self.store.db.execute(
            """SELECT checkpoint_h,
                      AVG(multiple) avg_mult,
                      SUM(CASE WHEN rugged=1 THEN 1 ELSE 0 END) rugs,
                      COUNT(*) n
               FROM outcomes
               WHERE recorded_ts >= ? AND multiple IS NOT NULL
               GROUP BY checkpoint_h ORDER BY checkpoint_h""",
            (since,),
        )
        rows = await cur.fetchall()
        if not rows:
            return "No outcome data yet."
        parts = []
        for r in rows:
            parts.append(
                f"+{r['checkpoint_h']}h: avg {r['avg_mult']:.2f}x "
                f"({r['rugs']}/{r['n']} rugged)"
            )
        return "Alert performance — " + " · ".join(parts)
