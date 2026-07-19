"""Phase 4 — the feedback loop, in one script.

After ~100 alerts you have a labeled dataset. This joins each alert's entry
snapshot with its +24h outcome and prints:

  * win rate (>=2x), rug rate, and average multiple, broken down by score band
  * a simple correlation table: which snapshot parameters actually separated
    winners from losers

Then you re-weight `config.yaml`'s scoring weights based on what *your* data
says — turning my starting guesses into your validated model.

Usage:  python analyze.py [--db screener.db] [--checkpoint 24]

Uses only the stdlib + sqlite3, so it runs anywhere the bot runs.
"""
from __future__ import annotations

import argparse
import sqlite3
from statistics import mean, pstdev


def _corr(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation, or None if undefined."""
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pts) < 3:
        return None
    xs2 = [p[0] for p in pts]
    ys2 = [p[1] for p in pts]
    sx, sy = pstdev(xs2), pstdev(ys2)
    if sx == 0 or sy == 0:
        return None
    mx, my = mean(xs2), mean(ys2)
    cov = mean((x - mx) * (y - my) for x, y in pts)
    return cov / (sx * sy)


PARAMS = [
    "top10_pct", "dev_pct", "liquidity_usd", "market_cap_usd", "vol_liq_ratio",
    "buys_sells_ratio", "holders", "holder_growth_1h", "price_change_1h",
    "rugcheck_score", "score_security", "score_traction", "score_social",
    "score_momentum", "score",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="screener.db")
    ap.add_argument("--checkpoint", type=int, default=24, help="outcome checkpoint hour")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        """SELECT a.score AS score, a.mint AS mint,
                  s.top10_pct, s.dev_pct, s.liquidity_usd, s.market_cap_usd,
                  s.vol_liq_ratio, s.buys_sells_ratio, s.holders,
                  s.holder_growth_1h, s.price_change_1h, s.rugcheck_score,
                  s.score_security, s.score_traction, s.score_social, s.score_momentum,
                  o.multiple, o.rugged
           FROM alerts a
           JOIN outcomes o ON o.alert_id = a.id AND o.checkpoint_h = ?
           LEFT JOIN snapshots s ON s.id = a.snapshot_id
           WHERE a.is_followup = 0 AND o.recorded_ts IS NOT NULL""",
        (args.checkpoint,),
    ).fetchall()

    if not rows:
        print(f"No recorded outcomes at +{args.checkpoint}h yet. "
              f"Let the bot run and alert first.")
        return

    n = len(rows)
    mults = [r["multiple"] for r in rows if r["multiple"] is not None]
    rugs = sum(1 for r in rows if r["rugged"])
    wins = sum(1 for r in rows if (r["multiple"] or 0) >= 2.0)

    print(f"=== Alert outcomes at +{args.checkpoint}h (n={n}) ===")
    print(f"Win rate (>=2x): {wins}/{n} = {100*wins/n:.0f}%")
    print(f"Rug rate:        {rugs}/{n} = {100*rugs/n:.0f}%")
    if mults:
        print(f"Avg multiple:    {mean(mults):.2f}x   median-ish spread {pstdev(mults):.2f}")
    print()

    # ── by score band ─────────────────────────────────────────────────
    print("=== By score band ===")
    bands = [(0, 70), (70, 80), (80, 90), (90, 101)]
    for lo, hi in bands:
        b = [r for r in rows if lo <= (r["score"] or 0) < hi]
        if not b:
            continue
        bm = [r["multiple"] for r in b if r["multiple"] is not None]
        bw = sum(1 for r in b if (r["multiple"] or 0) >= 2.0)
        br = sum(1 for r in b if r["rugged"])
        print(f"  {lo:>3}-{hi-1:<3}: n={len(b):<3} win={100*bw/len(b):>3.0f}% "
              f"rug={100*br/len(b):>3.0f}% avg={mean(bm) if bm else 0:.2f}x")
    print()

    # ── correlation of each parameter with the outcome multiple ───────
    print("=== Correlation with +{}h multiple (higher |r| = more predictive) ==="
          .format(args.checkpoint))
    ys = [r["multiple"] for r in rows]
    corrs = []
    for p in PARAMS:
        c = _corr([r[p] for r in rows], ys)
        if c is not None:
            corrs.append((p, c))
    for p, c in sorted(corrs, key=lambda t: -abs(t[1])):
        bar = "#" * int(abs(c) * 30)
        sign = "+" if c >= 0 else "-"
        print(f"  {p:<18} {sign}{abs(c):.2f}  {bar}")

    print()
    print("Read this as: parameters at the top separated winners from losers in")
    print("YOUR data. Nudge config.yaml scoring weights toward what actually")
    print("predicted, then keep collecting. Data over vibes.")


if __name__ == "__main__":
    main()
