"""Telegram transport — alerts to your phone via the Bot API.

Free. Create a bot with @BotFather, message it once, then read your chat id
from https://api.telegram.org/bot<TOKEN>/getUpdates. We use raw httpx calls to
the Bot API (no python-telegram-bot dependency needed for outbound messages).
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("deliver.telegram")


class TelegramSender:
    def __init__(self, bot_token: Optional[str], chat_id: Optional[str],
                 client: Optional[httpx.AsyncClient] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @retry(wait=wait_exponential(multiplier=1, min=1, max=15), stop=stop_after_attempt(3),
           reraise=False)
    async def _post(self, method: str, payload: dict) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        r = await self._client.post(url, json=payload)
        if r.status_code == 429:
            retry_after = (r.json().get("parameters") or {}).get("retry_after", 3)
            raise httpx.HTTPError(f"429 retry_after={retry_after}")
        r.raise_for_status()

    async def send_alert(self, alert: dict) -> None:
        if not self.enabled:
            return
        links = " | ".join(f"[{k}]({v})" for k, v in alert["links"].items())
        text = (
            f"{alert['text']}\n\n{links}\n"
            f"`{alert['mint']}`"          # tap-to-copy on mobile
        )
        try:
            await self._post("sendMessage", {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            })
        except Exception as e:  # noqa: BLE001 — delivery must never crash the bot
            log.error("Telegram send failed: %s", e)

    async def send_text(self, text: str) -> None:
        """Plain notifications: heartbeat warnings, daily summary."""
        if not self.enabled:
            return
        try:
            await self._post("sendMessage", {
                "chat_id": self.chat_id, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True,
            })
        except Exception as e:  # noqa: BLE001
            log.error("Telegram send_text failed: %s", e)
