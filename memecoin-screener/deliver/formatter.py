"""One alert format, rendered for two transports.

Every alert must let you decide in ~30 seconds without opening ten tabs:
score, age, the three security checkmarks, and one honest ⚠️ line for anything
borderline — plus links out to DexScreener / RugCheck / Solscan so YOU verify.

`render_text()` produces a plain-text body used by Telegram (as-is) and Discord
(inside a code block). Links and the copyable mint are appended.
"""
from __future__ import annotations

from typing import Optional

from core.config import Config
from core.models import ScoreBreakdown, TokenSnapshot


def _fmt_usd(x: Optional[float]) -> str:
    if x is None:
        return "?"
    if x >= 1_000_000:
        return f"${x/1_000_000:.1f}M"
    if x >= 1_000:
        return f"${x/1_000:.0f}k"
    return f"${x:.0f}"


def _fmt_age(minutes: Optional[float]) -> str:
    if minutes is None:
        return "age ?"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _check(v: Optional[bool]) -> str:
    if v is True:
        return "✅"
    if v is False:
        return "❌"
    return "❔"


def _score_emoji(score: float, threshold: float) -> str:
    if score >= threshold + 15:
        return "🟢"
    if score >= threshold:
        return "🟡"
    return "🔴"


def _warnings(snap: TokenSnapshot) -> list[str]:
    w: list[str] = []
    sec = snap.security
    if sec.bundled_snipers:
        w.append(f"{sec.bundled_snipers} wallet(s) bought in the same block at launch")
    if sec.dev_pct is not None and sec.dev_pct >= 3:
        w.append(f"dev still holds {sec.dev_pct:.1f}%")
    if sec.metadata_mutable:
        w.append("metadata is mutable (name/logo can change)")
    vlr = snap.pair.vol_liq_ratio
    if vlr is not None and vlr > 15:
        w.append(f"vol/liq {vlr:.0f}x — possible wash trading")
    if sec.rugcheck_label and sec.rugcheck_label.lower() in ("warning", "danger"):
        w.append(f"RugCheck flag: {sec.rugcheck_label}")
    return w


def build_alert(snap: TokenSnapshot, breakdown: ScoreBreakdown, cfg: Config,
                is_followup: bool, reason: str) -> dict:
    """Return {'text', 'links', 'mint', 'title'} pieces for the transports."""
    pair = snap.pair
    sec = snap.security
    th = cfg.scoring.alert_threshold
    emoji = _score_emoji(breakdown.total, th)

    sym = snap.symbol or "?"
    name = snap.name or ""
    header = f"{emoji} SCORE {breakdown.total:.0f} — ${sym}"
    if name:
        header += f" ({name})"
    if is_followup:
        header = f"🔁 FOLLOW-UP ({reason}) — ${sym}: now {breakdown.total:.0f}"

    age = _fmt_age(snap.age_minutes)
    grad = ""
    if snap.migrated and snap.migrated_ts:
        mins = max(0, int((snap.ts - snap.migrated_ts) / 60))
        grad = f" · graduated pump.fun {mins}m ago"
    line_age = f"Age {age}{grad}"

    lp = "burned" if (sec.lp_lock_days is None and sec.lp_secured) else (
        "locked" if sec.lp_secured else "unsecured")
    vlr = pair.vol_liq_ratio
    line_liq = (
        f"💧 Liq {_fmt_usd(pair.liquidity_usd)} ({lp}) · "
        f"MC {_fmt_usd(pair.market_cap_usd or pair.fdv_usd)} · "
        f"Vol/Liq {vlr:.1f}x" if vlr is not None else
        f"💧 Liq {_fmt_usd(pair.liquidity_usd)} ({lp}) · "
        f"MC {_fmt_usd(pair.market_cap_usd or pair.fdv_usd)}"
    )

    bsr = pair.buys_sells_ratio
    pc = pair.price_change_1h
    holders = f"holders {pair.holders:,}" if pair.holders else "holders ?"
    if snap.holder_growth_1h:
        holders += f" (+{snap.holder_growth_1h:.0f}/h)"
    line_trend = (
        f"📈 1h: {'+' if (pc or 0) >= 0 else ''}{pc:.0f}% · " if pc is not None else "📈 "
    ) + (f"buys/sells {bsr:.1f} · " if bsr is not None else "") + holders

    top10 = f"Top10 {sec.top10_pct:.0f}%" if sec.top10_pct is not None else "Top10 ?"
    dev = f"Dev {sec.dev_pct:.1f}%" if sec.dev_pct is not None else ""
    rc = f"RugCheck: {sec.rugcheck_label}" if sec.rugcheck_label else ""
    line_sec = (
        f"🔒 Mint {_check(sec.mint_revoked)} Freeze {_check(sec.freeze_revoked)} "
        f"{top10}" + (f" · {dev}" if dev else "") + (f" · {rc}" if rc else "")
    )

    body = "\n".join([header, line_age, "", line_liq, line_trend, line_sec])

    warns = _warnings(snap)
    if warns:
        body += "\n\n⚠️ Watch: " + "; ".join(warns)

    links = {
        "DexScreener": cfg.delivery.links["dexscreener"].format(mint=snap.mint),
        "RugCheck": cfg.delivery.links["rugcheck"].format(mint=snap.mint),
        "Solscan": cfg.delivery.links["solscan"].format(mint=snap.mint),
    }
    return {"title": header, "text": body, "links": links, "mint": snap.mint}
