"""Helius RPC client — ground truth from the chain.

We trust the chain for the facts that matter most and are cheap to verify
directly, rather than taking an aggregator's word:

  * getAccountInfo (jsonParsed) on the mint  -> mint & freeze authority
  * getTokenSupply                            -> total supply (for % math)
  * getTokenLargestAccounts                   -> top holder concentration

Free tier: 1M credits/mo, 10 req/s. We stay under 10 req/s via a limiter.

Concentration note: getTokenLargestAccounts returns the 20 largest *token
accounts*. Some are the LP vault and the burn/incinerator address; those are
excluded from the "top-10 non-LP wallet" concentration figure. We can't always
identify the LP vault purely from RPC, so we accept the largest known pool
address (from DexScreener) as an exclusion when provided.
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

log = logging.getLogger("enrich.helius")

# Known non-holder addresses to always exclude from concentration.
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",  # incinerator (burn)
}


class _Retryable(Exception):
    pass


class HeliusMintInfo:
    def __init__(self, mint_authority: Optional[str], freeze_authority: Optional[str],
                 supply: Optional[float], decimals: Optional[int],
                 is_initialized: bool):
        self.mint_authority = mint_authority
        self.freeze_authority = freeze_authority
        self.supply = supply
        self.decimals = decimals
        self.is_initialized = is_initialized

    @property
    def mint_revoked(self) -> bool:
        return self.mint_authority is None

    @property
    def freeze_revoked(self) -> bool:
        return self.freeze_authority is None


class HeliusClient:
    def __init__(self, rpc_url: str, limiter: AsyncRateLimiter,
                 client: Optional[httpx.AsyncClient] = None):
        if not rpc_url:
            raise ValueError("HELIUS_RPC_URL is required")
        self.rpc_url = rpc_url
        self.limiter = limiter
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None
        self._id = 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @retry(
        retry=retry_if_exception_type(_Retryable),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _rpc(self, method: str, params: list) -> Any:
        await self.limiter.acquire()
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        try:
            r = await self._client.post(self.rpc_url, json=payload)
        except httpx.HTTPError as e:
            raise _Retryable(str(e)) from e
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retryable(f"{r.status_code}")
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            # some RPC errors are transient (node behind); retry a few times
            raise _Retryable(str(body["error"]))
        return body.get("result")

    async def get_mint_info(self, mint: str) -> Optional[HeliusMintInfo]:
        result = await self._rpc(
            "getAccountInfo", [mint, {"encoding": "jsonParsed"}]
        )
        value = (result or {}).get("value") if isinstance(result, dict) else None
        if not value:
            return None
        parsed = (((value.get("data") or {}).get("parsed")) or {})
        info = parsed.get("info") or {}
        supply = info.get("supply")
        decimals = info.get("decimals")
        supply_f = None
        if supply is not None and decimals is not None:
            try:
                supply_f = int(supply) / (10 ** int(decimals))
            except (TypeError, ValueError):
                supply_f = None
        return HeliusMintInfo(
            mint_authority=info.get("mintAuthority"),
            freeze_authority=info.get("freezeAuthority"),
            supply=supply_f,
            decimals=decimals,
            is_initialized=bool(info.get("isInitialized", True)),
        )

    async def top10_pct(
        self,
        mint: str,
        total_supply: Optional[float] = None,
        exclude: Optional[set[str]] = None,
    ) -> Optional[float]:
        """Percent of supply held by the 10 largest NON-LP, non-burn accounts.

        `exclude` should contain any known LP vault / pool token-account owners
        (e.g. the DexScreener pair address). Burn addresses are always excluded.
        """
        exclusions = set(BURN_ADDRESSES)
        if exclude:
            exclusions |= exclude

        result = await self._rpc("getTokenLargestAccounts", [mint])
        accounts = (result or {}).get("value") if isinstance(result, dict) else None
        if not accounts:
            return None

        if total_supply is None:
            info = await self.get_mint_info(mint)
            total_supply = info.supply if info else None
        if not total_supply:
            return None

        held = []
        for acc in accounts:
            addr = acc.get("address")
            if addr in exclusions:
                continue
            amt = acc.get("uiAmount")
            if amt is None:
                amt_str = (acc.get("amount"), acc.get("decimals"))
                try:
                    amt = int(amt_str[0]) / (10 ** int(amt_str[1]))
                except (TypeError, ValueError):
                    continue
            held.append(float(amt))

        top10 = sorted(held, reverse=True)[:10]
        pct = 100.0 * sum(top10) / total_supply
        return round(pct, 2)
