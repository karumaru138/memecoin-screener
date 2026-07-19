"""DexScreener API client — the market-data workhorse.

Free, no auth, **60 req/min shared** across all endpoints. That single limit is
the binding constraint of the whole screener, so every call routes through one
shared AsyncRateLimiter (budgeted at 45/min in config for headroom).

Endpoints used:
  * /token-pairs/v1/solana/{mint}      -> all pairs for a token
  * /latest/dex/pairs/solana/{pairId}  -> stats for one pair
  * /latest/dex/search?q=              -> search
  * /token-profiles/latest/v1          -> latest profiles (socials/paid)
  * /token-boosts/latest/v1            -> boosted tokens (paid visibility)
  * /metas/trending/v1                 -> trending memecoin metas
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.models import PairStats
from core.ratelimit import AsyncRateLimiter

log = logging.getLogger("enrich.dexscreener")

BASE = "https://api.dexscreener.com"


class _Retryable(Exception):
    pass


class DexScreenerClient:
    def __init__(self, limiter: AsyncRateLimiter, client: Optional[httpx.AsyncClient] = None):
        self.limiter = limiter
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception_type(_Retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get(self, path: str, params: dict | None = None) -> Any:
        await self.limiter.acquire()
        try:
            r = await self._client.get(f"{BASE}{path}", params=params)
        except httpx.HTTPError as e:
            raise _Retryable(str(e)) from e
        if r.status_code == 429:
            # honor Retry-After if present; tenacity handles the wait otherwise
            log.warning("DexScreener 429 on %s", path)
            raise _Retryable("429 rate limited")
        if r.status_code >= 500:
            raise _Retryable(f"{r.status_code} server error")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ── pair stats for a specific mint ────────────────────────────────
    async def pair_stats(self, mint: str) -> Optional[PairStats]:
        """Return stats for the token's deepest-liquidity Solana pair."""
        data = await self._get(f"/token-pairs/v1/solana/{mint}")
        pairs = _as_pair_list(data)
        if not pairs:
            # fallback to search
            data = await self._get("/latest/dex/search", {"q": mint})
            pairs = _as_pair_list(data)
        if not pairs:
            return None
        # choose the pair with the most USD liquidity
        best = max(pairs, key=lambda p: _liq(p))
        return _pair_to_stats(best)

    # ── social / paid profile detection ───────────────────────────────
    async def latest_profiles(self) -> list[dict]:
        data = await self._get("/token-profiles/latest/v1")
        return data if isinstance(data, list) else []

    async def latest_boosts(self) -> list[dict]:
        data = await self._get("/token-boosts/latest/v1")
        return data if isinstance(data, list) else []

    async def trending_metas(self) -> list[dict]:
        data = await self._get("/metas/trending/v1")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("metas") or data.get("data") or []
        return []


# ── parsing helpers (defensive: DexScreener shapes drift) ─────────────

def _as_pair_list(data: Any) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("pairs"), list):
            return data["pairs"]
    return []


def _liq(pair: dict) -> float:
    liq = pair.get("liquidity") or {}
    return float(liq.get("usd") or 0)


def _pair_to_stats(p: dict) -> PairStats:
    liq = p.get("liquidity") or {}
    txns = p.get("txns") or {}
    vol = p.get("volume") or {}
    price_change = p.get("priceChange") or {}

    h1 = txns.get("h1") or {}
    buys = h1.get("buys")
    sells = h1.get("sells")

    info = p.get("info") or {}
    # socials present?  DexScreener puts them under info.socials / info.websites
    socials = info.get("socials") or []
    websites = info.get("websites") or []
    has_socials = bool(socials or websites)

    stats = PairStats(
        pair_address=p.get("pairAddress"),
        price_usd=_f(p.get("priceUsd")),
        liquidity_usd=_f(liq.get("usd")),
        market_cap_usd=_f(p.get("marketCap")),
        fdv_usd=_f(p.get("fdv")),
        vol24h_usd=_f(vol.get("h24")),
        buys_1h=int(buys) if buys is not None else None,
        sells_1h=int(sells) if sells is not None else None,
        price_change_1h=_f(price_change.get("h1")),
        holders=None,   # DexScreener doesn't expose holders; Helius/Birdeye do
    )
    # stash socials flag on the object via attribute for the caller (kept off
    # the pydantic model to avoid confusing PairStats with social data)
    stats._has_socials = has_socials  # type: ignore[attr-defined]
    # pump.fun tokens carry a launch timestamp we can use for age
    stats._pair_created_ms = p.get("pairCreatedAt")  # type: ignore[attr-defined]
    return stats


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
