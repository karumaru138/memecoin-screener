"""Solana Memecoin Screener — asyncio entrypoint.

One process, one event loop, several supervised tasks:

  * PumpPortal consumer   — real-time new tokens + migrations (one websocket)
  * DexScreener feed      — trending metas / profiles / boosts (1/min)
  * event router          — drains the ingest queue into the watchlist
  * scheduler             — every few seconds, process tokens that are due
  * outcome tracker       — records +1h/+6h/+24h price after each alert
  * monitor               — heartbeat + daily summary

All state lives in SQLite, so a crash-restart resumes cleanly. Ctrl-C / SIGTERM
triggers a graceful shutdown.

Run:  python main.py            (uses ./config.yaml and ./.env)
"""
from __future__ import annotations

import asyncio
import logging
import signal

import httpx

from core.config import load_config, load_secrets
from core.monitor import Monitor
from core.ratelimit import AsyncRateLimiter
from core.watchlist import Watchlist
from db.store import Store
from deliver.discord import DiscordSender
from deliver.hub import DeliveryHub
from deliver.telegram import TelegramSender
from enrich.dexscreener import DexScreenerClient
from enrich.enricher import Enricher
from enrich.helius import HeliusClient
from enrich.rugcheck import RugCheckClient
from ingest.dexscreener_poll import DexScreenerFeed
from ingest.pumpportal import PumpPortalConsumer
from logging_setup import setup_logging
from outcome.tracker import OutcomeTracker

log = logging.getLogger("main")

SCHEDULER_TICK_SECONDS = 3
OUTCOME_TICK_SECONDS = 60
MONITOR_TICK_SECONDS = 60


class App:
    def __init__(self, config_path: str = "config.yaml", env_path: str = ".env"):
        self.cfg = load_config(config_path)
        self.secrets = load_secrets(env_path)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def setup(self) -> None:
        setup_logging(self.cfg.runtime.log_path)
        log.info("Starting memecoin screener")

        if not self.secrets.helius_rpc_url:
            raise SystemExit(
                "HELIUS_RPC_URL is required (copy .env.example -> .env and fill it in)."
            )

        # ── shared HTTP + rate limiters ───────────────────────────────
        self.http = httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": "memecoin-screener/1.0"}
        )
        dex_limiter = AsyncRateLimiter.per_minute(self.cfg.pacing.dexscreener_budget_rpm)
        helius_limiter = AsyncRateLimiter(rate_per_sec=self.cfg.pacing.helius_rps)
        rugcheck_limiter = AsyncRateLimiter(rate_per_sec=2.0)  # gentle; respect 429s

        # ── clients ───────────────────────────────────────────────────
        self.dex = DexScreenerClient(dex_limiter, client=self.http)
        self.helius = HeliusClient(self.secrets.helius_rpc_url, helius_limiter, client=self.http)
        self.rugcheck = RugCheckClient(rugcheck_limiter,
                                       api_key=self.secrets.rugcheck_api_key, client=self.http)

        # ── store ─────────────────────────────────────────────────────
        self.store = Store(self.cfg.runtime.db_path)
        await self.store.open()

        # ── ingestion ─────────────────────────────────────────────────
        self.ingest_q: asyncio.Queue[dict] = asyncio.Queue(maxsize=10000)
        self.pumpportal = PumpPortalConsumer(self.ingest_q, api_key=self.secrets.pumpportal_api_key)
        self.feed = DexScreenerFeed(self.dex, self.ingest_q)

        # ── enrichment ────────────────────────────────────────────────
        self.enricher = Enricher(self.dex, self.helius, self.rugcheck, self.feed)

        # ── delivery ──────────────────────────────────────────────────
        telegram = TelegramSender(
            self.secrets.telegram_bot_token, self.secrets.telegram_chat_id, client=self.http)
        discord = DiscordSender(self.secrets.discord_webhook_url, client=self.http)
        self.hub = DeliveryHub(self.cfg, telegram, discord)

        # ── core ──────────────────────────────────────────────────────
        self.watchlist = Watchlist(self.store, self.enricher, self.cfg, self.hub.send_alert)
        self.outcomes = OutcomeTracker(self.store, self.dex)
        self.monitor = Monitor(self.cfg, self.store, self.hub,
                               last_event_ts_getter=lambda: self.pumpportal.last_event_ts)

        self._log_startup_report()

    def _log_startup_report(self) -> None:
        s = self.secrets
        def ok(x): return "set" if x else "MISSING"
        log.info("Config: threshold=%.0f weights=%s dry_run=%s",
                 self.cfg.scoring.alert_threshold, self.cfg.scoring.weights,
                 self.cfg.runtime.alert_dry_run)
        log.info("Secrets: helius=%s pumpportal=%s rugcheck=%s telegram=%s discord=%s",
                 ok(s.helius_rpc_url), ok(s.pumpportal_api_key), ok(s.rugcheck_api_key),
                 ok(s.telegram_bot_token and s.telegram_chat_id), ok(s.discord_webhook_url))
        if not self.rugcheck.enabled:
            log.warning("RugCheck disabled (no API key) — risk component treated as neutral")

    # ── supervised loops ──────────────────────────────────────────────
    async def _router_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = await asyncio.wait_for(self.ingest_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self.watchlist.on_event(event)
            except Exception as e:  # noqa: BLE001
                log.exception("router failed on %s: %s", event.get("mint"), e)

    async def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = await self.watchlist.process_due(limit=25)
                if n:
                    log.debug("processed %d due tokens", n)
            except Exception as e:  # noqa: BLE001
                log.exception("scheduler tick failed: %s", e)
            await self._sleep_or_stop(SCHEDULER_TICK_SECONDS)

    async def _outcome_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.outcomes.process_due()
            except Exception as e:  # noqa: BLE001
                log.exception("outcome tick failed: %s", e)
            await self._sleep_or_stop(OUTCOME_TICK_SECONDS)

    async def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.monitor.check_heartbeat()
                await self.monitor.maybe_daily_summary()
            except Exception as e:  # noqa: BLE001
                log.exception("monitor tick failed: %s", e)
            await self._sleep_or_stop(MONITOR_TICK_SECONDS)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ── lifecycle ─────────────────────────────────────────────────────
    async def run(self) -> None:
        await self.setup()
        self._tasks = [
            asyncio.create_task(self.pumpportal.run(), name="pumpportal"),
            asyncio.create_task(self.feed.run(), name="dexfeed"),
            asyncio.create_task(self._router_loop(), name="router"),
            asyncio.create_task(self._scheduler_loop(), name="scheduler"),
            asyncio.create_task(self._outcome_loop(), name="outcomes"),
            asyncio.create_task(self._monitor_loop(), name="monitor"),
        ]
        if self.hub.telegram.enabled or self.hub.discord.enabled:
            await self.hub.notify("🚀 Memecoin screener online.")
        await self._stop.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        log.info("Shutting down…")
        self.pumpportal.stop()
        self.feed.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.dex.aclose()
        await self.helius.aclose()
        await self.rugcheck.aclose()
        await self.hub.aclose()
        await self.http.aclose()
        await self.store.close()
        log.info("Bye.")

    def request_stop(self, *_: object) -> None:
        log.info("Stop requested")
        self._stop.set()


async def _amain() -> None:
    app = App()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:  # e.g. Windows
            pass
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
