"""Discord transport — alerts to your server via a channel webhook.

Free. Channel settings → Integrations → Webhooks → New Webhook, paste the URL
into DISCORD_WEBHOOK_URL. We POST a rich embed so alerts are scannable.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("deliver.discord")


def _color(score: float, threshold: float) -> int:
    if score >= threshold + 15:
        return 0x2ECC71  # green
    if score >= threshold:
        return 0xF1C40F  # yellow
    return 0xE74C3C      # red


class DiscordSender:
    def __init__(self, webhook_url: Optional[str],
                 client: Optional[httpx.AsyncClient] = None):
        self.webhook_url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=1, min=1, max=15), stop=stop_after_attempt(3),
           reraise=False)
    async def _post(self, payload: dict) -> None:
        r = await self._client.post(self.webhook_url, json=payload)
        if r.status_code == 429:
            raise httpx.HTTPError("429 rate limited")
        r.raise_for_status()

    async def send_alert(self, alert: dict, score: float, threshold: float) -> None:
        if not self.enabled:
            return
        links = " · ".join(f"[{k}]({v})" for k, v in alert["links"].items())
        embed = {
            "title": alert["title"],
            "description": f"```\n{alert['text']}\n```\n{links}\n`{alert['mint']}`",
            "color": _color(score, threshold),
        }
        try:
            await self._post({"embeds": [embed]})
        except Exception as e:  # noqa: BLE001
            log.error("Discord send failed: %s", e)

    async def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            await self._post({"content": text[:1900]})
        except Exception as e:  # noqa: BLE001
            log.error("Discord send_text failed: %s", e)
