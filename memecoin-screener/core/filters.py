"""Hard filters — the ✋ kill switches.

Pure functions: `TokenSnapshot` + `HardFilters` config in, `FilterResult` out.
No I/O, no globals. This makes them trivially unit-testable and backtestable
against recorded snapshots.

Philosophy on unknowns: a hard filter should only REJECT on a fact we actually
know to be bad. If a required security fact is unknown (None), we do NOT pass
the token — but we express that as "not yet decidable" rather than a permanent
reject, so the watchlist keeps enriching it. `evaluate()` returns a
FilterResult plus a `decidable` flag for that distinction.
"""
from __future__ import annotations

from core.config import HardFilters
from core.models import FilterResult, TokenSnapshot


class HardFilterVerdict(FilterResult):
    # False when a *required* fact is still unknown (keep enriching, don't
    # mark REJECTED permanently). True once every required fact is known.
    decidable: bool = True


def evaluate(snap: TokenSnapshot, hf: HardFilters) -> HardFilterVerdict:
    failures: list[tuple[str, str]] = []
    unknown_required = False

    sec = snap.security
    pair = snap.pair

    # ── mint authority ────────────────────────────────────────────────
    if hf.require_mint_revoked:
        if sec.mint_revoked is None:
            unknown_required = True
        elif not sec.mint_revoked:
            failures.append(("mint_authority", "Mint authority still live"))

    # ── freeze authority ──────────────────────────────────────────────
    if hf.require_freeze_revoked:
        if sec.freeze_revoked is None:
            unknown_required = True
        elif not sec.freeze_revoked:
            failures.append(("freeze_authority", "Freeze authority still live"))

    # ── LP burned/locked ──────────────────────────────────────────────
    if hf.require_lp_secured:
        if sec.lp_secured is None:
            unknown_required = True
        elif not sec.lp_secured:
            failures.append(("lp_status", "LP not burned/locked"))
        elif sec.lp_lock_days is not None and sec.lp_lock_days < hf.lp_min_lock_days:
            # burned LP reports lock_days as very large / None; only locked
            # LPs carry a finite lock, so enforce the minimum here.
            failures.append(
                ("lp_lock", f"LP lock {sec.lp_lock_days:.0f}d < {hf.lp_min_lock_days}d")
            )

    # ── top-10 concentration ──────────────────────────────────────────
    if sec.top10_pct is None:
        unknown_required = True
    elif sec.top10_pct > hf.max_top10_pct:
        failures.append(
            ("top10_concentration",
             f"Top-10 hold {sec.top10_pct:.1f}% > {hf.max_top10_pct:.0f}%")
        )

    # ── liquidity floor ───────────────────────────────────────────────
    if pair.liquidity_usd is None:
        unknown_required = True
    elif pair.liquidity_usd < hf.min_liquidity_usd:
        failures.append(
            ("liquidity", f"Liquidity ${pair.liquidity_usd:,.0f} < ${hf.min_liquidity_usd:,.0f}")
        )

    # ── age window ────────────────────────────────────────────────────
    age_min = snap.age_minutes
    if age_min is None:
        unknown_required = True
    else:
        if age_min < hf.min_age_minutes:
            failures.append(
                ("min_age", f"Age {age_min:.0f}m < {hf.min_age_minutes:.0f}m")
            )
        if age_min > hf.max_age_hours * 60:
            failures.append(
                ("max_age", f"Age {age_min/60:.1f}h > {hf.max_age_hours:.0f}h")
            )

    # ── RugCheck hard reject (optional) ───────────────────────────────
    if hf.rugcheck_hard_reject_score is not None and sec.rugcheck_score is not None:
        if sec.rugcheck_score >= hf.rugcheck_hard_reject_score:
            failures.append(
                ("rugcheck", f"RugCheck risk {sec.rugcheck_score:.0f} too high")
            )

    passed = not failures and not unknown_required
    return HardFilterVerdict(
        passed=passed,
        failures=failures,
        decidable=not unknown_required,
    )


# Failures that should be treated as permanent (never re-check).
PERMANENT_FAILURES = {"mint_authority", "freeze_authority", "lp_status", "lp_lock"}


def is_permanent_reject(verdict: FilterResult) -> bool:
    """Authority/LP failures are structural and don't heal; concentration and
    liquidity can change, so those get a re-check later."""
    return any(name in PERMANENT_FAILURES for name, _ in verdict.failures)
