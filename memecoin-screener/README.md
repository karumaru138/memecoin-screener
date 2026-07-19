# Solana Memecoin Screener

A bot that watches new and trending Solana memecoins around the clock, filters
them through the parameters that predict survival vs. rug, scores the
survivors, and pushes high-signal alerts to **Telegram** and **Discord**.

**You make every trade decision. The bot never touches your money.** It is a
screener, not an auto-trader — it compresses ~10,000 daily launches into a
handful of candidates worth a human look, then links you out to DexScreener /
RugCheck / Solscan so *you* verify before deciding.

> Not financial advice. Most memecoin alerts lose. See `Honest limitations` at
> the bottom, and the operating playbook — position sizing and pre-planned
> exits are what make a screener useful.

---

## How it works

```
PumpPortal WS  ─┐
  new launches  ├─► WATCHLIST (SQLite state machine) ─► ENRICH ─► FILTER+SCORE ─► ALERT
  migrations    │     NEW→WATCHING→CANDIDATE→ALERTED    Helius   hard kill      Telegram
DexScreener  ──┘                    ↘REJECTED ↘EXPIRED   RugCheck  switches +    Discord
  trending/profiles                                      DexScr.   0-100 score
```

1. **Ingest** every relevant event in real time (new launches, pump.fun
   graduations, boosted/trending tokens).
2. **Filter** with hard *kill switches* — objective on-chain facts that
   disqualify a token instantly (mint authority live, LP unlocked, one cohort
   holds >25%, too young, too illiquid).
3. **Score** survivors 0–100 across security / traction / social / momentum,
   and alert only above a threshold you set.

The watchlist state machine is what keeps the whole thing inside free-tier
rate limits: only a few dozen tokens are ever actively enriched at once, not
thousands.

---

## Quick start

```bash
# 1. install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. configure secrets
cp .env.example .env
$EDITOR .env          # fill in at least HELIUS_RPC_URL + Telegram (or Discord)

# 3. (optional) tune thresholds
$EDITOR config.yaml

# 4. run
python main.py
```

You need, at minimum, a **Helius RPC URL** (free tier) and **one** delivery
channel (Telegram bot token + chat id, or a Discord webhook). PumpPortal and
RugCheck keys are optional but recommended — see `.env.example` for where to get
each. Without a RugCheck key its risk component is treated as neutral.

### Getting the keys

| Key | Where | Cost |
|---|---|---|
| `HELIUS_RPC_URL` | https://helius.dev → copy your mainnet RPC URL | Free (1M credits/mo) |
| `PUMPPORTAL_API_KEY` | https://pumpportal.fun (link wallet ≥0.02 SOL) | Free data stream |
| `RUGCHECK_API_KEY` | https://rugcheck.xyz | Free tier |
| `TELEGRAM_BOT_TOKEN` / `_CHAT_ID` | @BotFather, then `/getUpdates` for chat id | Free |
| `DISCORD_WEBHOOK_URL` | Channel → Integrations → Webhooks | Free |

---

## Configuration

Everything tunable lives in **`config.yaml`** — thresholds, scoring weights,
pacing, rate-limit budget, alert cap. Nothing is hardcoded. The most important
knobs:

- `scoring.alert_threshold` (default **70**) — raise it if you get too many
  alerts, lower it if too few.
- `scoring.weights` — must sum to 1.0. Start with the defaults; **retune from
  real outcomes** (see Feedback loop below).
- `delivery.max_alerts_per_hour` (default **6**) — a hard cap. Alert fatigue
  kills the tool; if you hit the cap the bot suppresses and warns you to raise
  the threshold.
- `runtime.alert_dry_run: true` — do everything except actually send alerts.
  Great for a first run to watch the firehose without pings.

---

## Project layout

```
config.yaml            all thresholds & weights (safe to commit)
.env                   API keys (gitignored)
main.py                asyncio entrypoint, supervises every task
ingest/                pumpportal.py (websocket) · dexscreener_poll.py (trending)
enrich/                helius.py · rugcheck.py · dexscreener.py · enricher.py
core/                  watchlist.py (state machine) · filters.py · scoring.py
                       · models.py · config.py · ratelimit.py · monitor.py
deliver/               telegram.py · discord.py · formatter.py · hub.py
outcome/               tracker.py (logs price +1h/+6h/+24h per alert)
analyze.py             Phase 4 feedback-loop analysis (stdlib only)
db/                    schema.sql · store.py
tests/                 filters, scoring, and store/state-machine unit tests
Dockerfile / docker-compose.yml / deploy/screener.service
```

`filters.py` and `scoring.py` are **pure functions** (snapshot in → verdict
out): no I/O, so they unit-test and backtest cleanly. That's where all your
future tuning happens.

---

## Deployment

Don't run it on your laptop — it must survive sleep, reboots, and your ISP.

**Docker (recommended):**
```bash
mkdir -p data
# in config.yaml set:  runtime.db_path: /app/data/screener.db
#                      runtime.log_path: /app/data/logs/screener.jsonl
docker compose up -d --build
docker compose logs -f
```

**Plain VPS + systemd:** see `deploy/screener.service` (a €4–8/mo Hetzner CX22
is plenty; this bot isn't latency-sensitive).

Either way: state lives in SQLite, so a crash-restart loses nothing but a few
minutes. The PumpPortal websocket reconnects with exponential backoff; every
REST call retries and honors 429s. A heartbeat pings you if the stream goes
silent for 15 min, plus a daily 9:00 summary.

Monthly cost: **VPS ~€5, everything else $0.**

---

## Testing

```bash
pytest -q          # 22 tests: hard filters, scoring, state machine
```

---

## The feedback loop (this is what matters)

The default scoring weights are *a sane starting point, not a validated model.*
The outcome tracker records every alert's price at +1h/+6h/+24h and whether it
rugged. After ~100 alerts:

```bash
python analyze.py --checkpoint 24
```

You get win/rug rates by score band and a correlation table of **which
parameters actually separated winners from losers in your data**. Nudge the
`config.yaml` weights toward what predicted, and keep collecting. That loop is
what turns starter guesses into *your* model.

---

## Operating playbook (the human half)

Alerts are **candidates, not calls.** A screener's edge dies at execution if
you skip discipline:

1. **Verify before buying.** Open the DexScreener + RugCheck links yourself.
   APIs lag; scammers adapt to popular filters.
2. **Position sizing is the whole game.** Only money you fully accept losing;
   fixed small size per trade. Never "this one feels different."
3. **Decide the exit before entry.** e.g. sell 50% at 2x, trail the rest,
   hard-stop at −50%. Pick your numbers *before* you buy.
4. **The bot keeps watching after you buy** — a follow-up alert ("top10 jumped
   to 40%", "LP change") is your fire alarm.
5. **Track P&L per score band** and let the data move your threshold.

---

## Honest limitations

- This finds candidates faster; it **does not create edge by itself.** Everyone
  screens on these same public parameters; sophisticated scammers engineer
  tokens to pass them. Hard filters remove the *lazy* rugs.
- **Most alerts will still lose.** The economics come from asymmetry (small
  losses, occasional multiples) — which only position sizing and exits capture.
- **Expect maintenance.** APIs change fields and limits; pump.fun has changed
  its migration mechanics before and will again. Budget ~an hour a week.
- **Small capital is a real handicap** — fees, slippage, and minimum sizes bite
  hardest at low bankrolls. Treat this as tuition-priced experimentation with
  money you can lose entirely.
- Not financial advice — this is an engineering tool plus publicly known risk
  practices.
