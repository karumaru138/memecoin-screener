"""Weighted 0–100 scoring model.

Pure functions. Runs only on tokens that already passed the hard filters, so
here we assume the basic security facts are present and reward/penalize on the
margins. Every sub-score is clamped to [0, 100]; the final blend uses the
configured weights.

    score = w_sec·security + w_trac·traction + w_soc·social + w_mom·momentum

These weights are a *sane starting point, not a validated model*. The outcome
tracker (Phase 4) is what turns them into real numbers.
"""
from __future__ import annotations

from core.config import Scoring
from core.models import ScoreBreakdown, TokenSnapshot


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def score_security(snap: TokenSnapshot, cfg: Scoring) -> tuple[float, list[str]]:
    """Start at 100, subtract penalties for residual risk."""
    s = cfg.security
    sec = snap.security
    score = 100.0
    notes: list[str] = []

    # dev/creator holdings
    if sec.dev_pct is not None:
        if sec.dev_pct >= s.dev_pct_hard:
            score -= s.dev_penalty_hard
            notes.append(f"dev holds {sec.dev_pct:.1f}% (−{s.dev_penalty_hard:.0f})")
        elif sec.dev_pct >= s.dev_pct_soft:
            score -= s.dev_penalty_soft
            notes.append(f"dev holds {sec.dev_pct:.1f}% (−{s.dev_penalty_soft:.0f})")

    # top-10 concentration: penalize per % over half the hard cap. The hard
    # filter already rejects above the cap, so this rewards *low* concentration.
    if sec.top10_pct is not None:
        # reference midpoint ~ half of a 25% cap = 12.5; anything above bleeds.
        over = sec.top10_pct - 12.5
        if over > 0:
            pen = over * s.top10_penalty_per_pct_over
            score -= pen
            notes.append(f"top10 {sec.top10_pct:.1f}% (−{pen:.0f})")

    # mutable metadata
    if sec.metadata_mutable:
        score -= s.metadata_mutable_penalty
        notes.append(f"metadata mutable (−{s.metadata_mutable_penalty:.0f})")

    # bundled snipers (insider launch signature)
    if sec.bundled_snipers:
        pen = s.bundled_sniper_penalty
        score -= pen
        notes.append(f"{sec.bundled_snipers} bundled snipers (−{pen:.0f})")

    # RugCheck: scale penalty by how risky (0..~50k+ scale, normalize to 0..1
    # against a nominal 50k "very risky" ceiling).
    if sec.rugcheck_score is not None:
        frac = _clamp(sec.rugcheck_score / 50000.0, 0.0, 1.0)
        pen = frac * s.rugcheck_max_penalty
        if pen >= 1:
            score -= pen
            label = sec.rugcheck_label or f"{sec.rugcheck_score:.0f}"
            notes.append(f"RugCheck {label} (−{pen:.0f})")

    return _clamp(score), notes


def score_traction(snap: TokenSnapshot, cfg: Scoring) -> tuple[float, list[str]]:
    t = cfg.traction
    pair = snap.pair
    score = 0.0
    notes: list[str] = []

    # liquidity band (award the highest band whose floor is met)
    if pair.liquidity_usd is not None:
        pts = 0.0
        for floor, band_pts in sorted(t.liquidity_bands):
            if pair.liquidity_usd >= floor:
                pts = band_pts
        if pts:
            score += pts
            notes.append(f"liq ${pair.liquidity_usd:,.0f} (+{pts:.0f})")

    # market-cap sweet spot
    mc = pair.market_cap_usd or pair.fdv_usd
    if mc is not None and t.mcap_sweet_low <= mc <= t.mcap_sweet_high:
        score += t.mcap_points
        notes.append(f"mcap ${mc:,.0f} in range (+{t.mcap_points:.0f})")

    # volume:liquidity in the organic band (too high == wash trading)
    vlr = pair.vol_liq_ratio
    if vlr is not None:
        if t.volliq_organic_low <= vlr <= t.volliq_organic_high:
            score += t.volliq_points
            notes.append(f"vol/liq {vlr:.1f}x organic (+{t.volliq_points:.0f})")
        elif vlr > t.volliq_wash_ratio:
            notes.append(f"vol/liq {vlr:.1f}x looks washed (+0)")

    # buy pressure
    bsr = pair.buys_sells_ratio
    if bsr is not None and bsr >= t.buys_sells_min:
        score += t.buys_sells_points
        notes.append(f"buys/sells {bsr:.2f} (+{t.buys_sells_points:.0f})")

    # holder growth rate: scale points by growth relative to a 200/h reference.
    if snap.holder_growth_1h is not None and snap.holder_growth_1h > 0:
        frac = _clamp(snap.holder_growth_1h / 200.0, 0.0, 1.0)
        pts = frac * t.holder_growth_points
        if pts >= 1:
            score += pts
            notes.append(f"+{snap.holder_growth_1h:.0f} holders/h (+{pts:.0f})")

    return _clamp(score), notes


def score_social(snap: TokenSnapshot, cfg: Scoring) -> tuple[float, list[str]]:
    s = cfg.social
    soc = snap.social
    score = 0.0
    notes: list[str] = []

    if soc.has_socials:
        score += s.live_socials_points
        notes.append(f"socials live (+{s.live_socials_points:.0f})")
    if soc.paid_profile:
        score += s.paid_profile_points
        notes.append(f"paid DexScreener profile (+{s.paid_profile_points:.0f})")
    if soc.active_meta:
        score += s.active_meta_points
        notes.append(f"active meta (+{s.active_meta_points:.0f})")
    if soc.smart_wallet_buys:  # Phase 3
        score += s.smart_wallet_points
        notes.append(f"{soc.smart_wallet_buys} smart-wallet buys (+{s.smart_wallet_points:.0f})")

    return _clamp(score), notes


def score_momentum(snap: TokenSnapshot, cfg: Scoring) -> tuple[float, list[str]]:
    """Reward acceleration, but cap a purely-vertical pump — vertical = late."""
    m = cfg.momentum
    pair = snap.pair
    score = 0.0
    notes: list[str] = []

    # 1h price change, scaled: +100% maps to full points, saturating.
    if pair.price_change_1h is not None and pair.price_change_1h > 0:
        frac = _clamp(pair.price_change_1h / 100.0, 0.0, 1.0)
        pts = frac * m.price_1h_points
        score += pts
        notes.append(f"1h +{pair.price_change_1h:.0f}% (+{pts:.0f})")

    # holder acceleration, scaled against a reference of +100/h change.
    if snap.holder_accel is not None and snap.holder_accel > 0:
        frac = _clamp(snap.holder_accel / 100.0, 0.0, 1.0)
        pts = frac * m.holder_accel_points
        score += pts
        notes.append(f"holder accel (+{pts:.0f})")

    # cap vertical pumps
    if pair.price_change_1h is not None and pair.price_change_1h > 150:
        score = min(score, m.vertical_cap)
        notes.append(f"vertical pump — capped at {m.vertical_cap:.0f}")

    return _clamp(score), notes


def score(snap: TokenSnapshot, cfg: Scoring) -> ScoreBreakdown:
    sec, sec_notes = score_security(snap, cfg)
    trac, trac_notes = score_traction(snap, cfg)
    soc, soc_notes = score_social(snap, cfg)
    mom, mom_notes = score_momentum(snap, cfg)

    w = cfg.weights
    total = (
        w["security"] * sec
        + w["traction"] * trac
        + w["social"] * soc
        + w["momentum"] * mom
    )
    return ScoreBreakdown(
        security=round(sec, 1),
        traction=round(trac, 1),
        social=round(soc, 1),
        momentum=round(mom, 1),
        total=round(total, 1),
        notes=sec_notes + trac_notes + soc_notes + mom_notes,
    )
