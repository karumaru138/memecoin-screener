"""Structured JSON-lines logging with daily rotation.

Every log line is one JSON object -> easy to grep/analyze. Filter rejections
log *which* filter fired (see watchlist), which is your tuning data.
"""
from __future__ import annotations

import json
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj)


def setup_logging(log_path: str = "logs/screener.jsonl", level: int = logging.INFO) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # Windows terminals default to a legacy code page (cp1252) that can't
    # print emoji/box-drawing chars — force stdout to UTF-8 so logs never crash.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
    except (AttributeError, ValueError):
        pass

    # human-readable to stdout
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)

    # JSON lines to file, rotated daily, 14 days kept (UTF-8 so non-ASCII is safe)
    fileh = TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=14, utc=True, encoding="utf-8"
    )
    fileh.setFormatter(JsonFormatter())
    root.addHandler(fileh)

    # quiet noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
