"""Delivery hub — fans one alert out to every enabled transport.

Exposes the `AlertSink` the watchlist calls, plus `notify()` for plain-text
operational messages (heartbeat warnings, daily summary).
"""
from __future__ import annotations

import logging

from core.config import Config
from core.models import ScoreBreakdown, TokenSnapshot
from deliver.discord import DiscordSender
from deliver.formatter import build_alert
from deliver.telegram import TelegramSender

log = logging.getLogger("deliver.hub")


class DeliveryHub:
    def __init__(self, cfg: Config, telegram: TelegramSender, discord: DiscordSender):
        self.cfg = cfg
        self.telegram = telegram
        self.discord = discord

    async def send_alert(self, snap: TokenSnapshot, breakdown: ScoreBreakdown,
                         is_followup: bool, reason: str) -> None:
        """The AlertSink signature the watchlist expects."""
        alert = build_alert(snap, breakdown, self.cfg, is_followup, reason)
        if self.cfg.delivery.telegram.enabled and self.telegram.enabled:
            await self.telegram.send_alert(alert)
        if self.cfg.delivery.discord.enabled and self.discord.enabled:
            await self.discord.send_alert(
                alert, breakdown.total, self.cfg.scoring.alert_threshold
            )

    async def notify(self, text: str) -> None:
        """Operational message to every enabled transport."""
        if self.cfg.delivery.telegram.enabled and self.telegram.enabled:
            await self.telegram.send_text(text)
        if self.cfg.delivery.discord.enabled and self.discord.enabled:
            await self.discord.send_text(text)

    async def aclose(self) -> None:
        await self.telegram.aclose()
        await self.discord.aclose()
