"""RugCheck client — aggregated third-party risk report.

Catches things we don't model ourselves: fake LP, copycat tokens, known scam
deployers, LP lock details. Used as a *component*, not gospel (the plan is
explicit about this).

  * Endpoint: GET /v1/tokens/{mint}/report
  * Free tier with an API key (sign up at rugcheck.xyz).
  * Respect 429 with backoff.

If no API key is configured, RugCheck is skipped gracefully: the report comes
back None and scoring treats the component as neutral.
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

from core.ratelimit import AsyncRateLimiter

log = logging.getLogger("enrich.rugcheck")

BASE = "https://api.rugcheck.xyz"


class _Retryable(Exception):
    pass


class RugCheckReport:
    def __init__(self, score: Optional[float], label: Optional[str],
                 lp_locked_pct: Optional[float], lp_locked: Optional[bool],
                 raw: dict):
        self.score = score              # higher = riskier (rugcheck convention)
        self.label = label              # "Good" / "Warning" / "Danger" etc.
        self.lp_locked_pct = lp_locked_pct
        self.lp_locked = lp_locked
        self.raw = raw


class RugCheckClient:
    def __init__(self, limiter: AsyncRateLimiter, api_key: Optional[str] = None,
                 client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key
        self.limiter = limiter
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception_type(_Retryable),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _get(self, path: str) -> Any:
        await self.limiter.acquire()
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            r = await self._client.get(f"{BASE}{path}", headers=headers)
        except httpx.HTTPError as e:
            raise _Retryable(str(e)) from e
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retryable(f"{r.status_code}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    async def report(self, mint: str) -> Optional[RugCheckReport]:
        if not self.enabled:
            return None
        try:
            data = await self._get(f"/v1/tokens/{mint}/report")
        except httpx.HTTPStatusError as e:
            log.warning("RugCheck report failed for %s: %s", mint, e)
            return None
        if not isinstance(data, dict):
            return None
        return _parse_report(data)


def _parse_report(data: dict) -> RugCheckReport:
    score = data.get("score_normalised")
    if score is None:
        score = data.get("score")

    # derive a human label if RugCheck didn't hand us one
    label = data.get("scoreLabel") or data.get("risk_level")
    if label is None and score is not None:
        try:
            s = float(score)
            label = "Good" if s < 10000 else "Warning" if s < 30000 else "Danger"
        except (TypeError, ValueError):
            label = None

    # LP lock: RugCheck exposes markets[].lp with lockedPct / a locked flag
    lp_locked_pct: Optional[float] = None
    lp_locked: Optional[bool] = None
    markets = data.get("markets") or []
    if markets:
        lp = (markets[0] or {}).get("lp") or {}
        lp_locked_pct = _f(lp.get("lpLockedPct"))
        if lp_locked_pct is not None:
            lp_locked = lp_locked_pct >= 90.0

    return RugCheckReport(
        score=_f(score),
        label=label,
        lp_locked_pct=lp_locked_pct,
        lp_locked=lp_locked,
        raw=data,
    )


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
