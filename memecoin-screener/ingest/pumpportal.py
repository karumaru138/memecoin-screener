"""PumpPortal websocket consumer.

Real-time source of new token launches and pump.fun -> Raydium/PumpSwap
migrations. Key operational facts (verified against the plan, July 2026):

  * Endpoint: wss://pumpportal.fun/api/data
  * Free methods: subscribeNewToken, subscribeMigration.
  * You may open ONLY ONE websocket connection — multiplex all subscriptions
    on it or risk a temporary ban. So this single consumer owns the socket.
  * The socket WILL drop. Reconnect with exponential backoff (1s -> 60s cap)
    and re-subscribe on every (re)connect.

Emits normalized events onto an asyncio.Queue for the rest of the app:
    {"type": "new"|"migration", "mint": str, "symbol": str|None,
     "name": str|None, "pair": str|None, "raw": {...}, "received_ts": float}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

log = logging.getLogger("ingest.pumpportal")

WS_URL = "wss://pumpportal.fun/api/data"

# Subscriptions to (re)send on every connect. Free methods only — the metered
# trade streams are deliberately omitted (DexScreener polling covers them).
_SUBSCRIPTIONS = [
    {"method": "subscribeNewToken"},
    {"method": "subscribeMigration"},
]


def _extract_mint(msg: dict) -> Optional[str]:
    for key in ("mint", "mintAddress", "ca", "address"):
        if msg.get(key):
            return msg[key]
    return None


def _classify(msg: dict) -> Optional[str]:
    """Return 'new', 'migration', or None for keepalives/acks."""
    txtype = (msg.get("txType") or msg.get("type") or "").lower()
    if "migrat" in txtype or msg.get("pool") == "raydium" and msg.get("signature"):
        return "migration"
    if txtype in ("create", "new") or msg.get("method") == "subscribeNewToken":
        return "new"
    # PumpPortal tags migration events with a marker; fall back on presence of
    # a pool/pair for a token we may already know.
    if msg.get("txType") == "migrate":
        return "migration"
    if _extract_mint(msg) and msg.get("marketCapSol") is not None and "migrat" in json.dumps(msg).lower():
        return "migration"
    if _extract_mint(msg):
        return "new"
    return None


def normalize(msg: dict) -> Optional[dict]:
    kind = _classify(msg)
    mint = _extract_mint(msg)
    if not kind or not mint:
        return None
    return {
        "type": kind,
        "mint": mint,
        "symbol": msg.get("symbol"),
        "name": msg.get("name"),
        "pair": msg.get("pool") or msg.get("pairAddress") or msg.get("marketId"),
        "raw": msg,
        "received_ts": time.time(),
    }


class PumpPortalConsumer:
    def __init__(self, out_queue: "asyncio.Queue[dict]", api_key: Optional[str] = None):
        self.out = out_queue
        self.api_key = api_key
        self._stop = asyncio.Event()
        self.last_event_ts: float = 0.0     # for the heartbeat monitor

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        url = WS_URL
        if self.api_key:
            url = f"{WS_URL}?api-key={self.api_key}"
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, max_queue=1024
                ) as ws:
                    log.info("PumpPortal connected")
                    backoff = 1.0
                    for sub in _SUBSCRIPTIONS:
                        await ws.send(json.dumps(sub))
                    await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — reconnect on anything
                log.warning("PumpPortal disconnected: %s — retry in %.0fs", e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)
        log.info("PumpPortal consumer stopped")

    async def _consume(self, ws) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            # ignore subscription acknowledgements
            if msg.get("message") and "successfully subscribed" in str(msg["message"]).lower():
                continue
            event = normalize(msg)
            if event is None:
                continue
            self.last_event_ts = event["received_ts"]
            await self.out.put(event)
