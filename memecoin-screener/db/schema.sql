-- ═══════════════════════════════════════════════════════════════════
--  Solana Memecoin Screener — SQLite schema
--  All bot state lives here so a crash-restart loses nothing.
-- ═══════════════════════════════════════════════════════════════════

PRAGMA journal_mode = WAL;      -- concurrent read while writing
PRAGMA foreign_keys = ON;

-- One row per token we've ever seen. `mint` is the Solana mint address.
CREATE TABLE IF NOT EXISTS tokens (
    mint            TEXT PRIMARY KEY,
    symbol          TEXT,
    name            TEXT,
    pair_address    TEXT,               -- primary DexScreener pair, once known
    state           TEXT NOT NULL DEFAULT 'NEW',
                                        -- NEW|WATCHING|CANDIDATE|ALERTED|REJECTED|EXPIRED
    created_at      REAL NOT NULL,      -- unix ts we first saw it
    launch_ts       REAL,              -- best estimate of on-chain launch time
    migrated_ts     REAL,              -- pump.fun graduation time, if seen
    next_check_ts   REAL NOT NULL DEFAULT 0,   -- scheduler due time
    last_check_ts   REAL,
    last_score      REAL,
    last_alert_score REAL,             -- score at the most recent alert sent
    reject_reason   TEXT,
    reject_ts       REAL,
    recheck_after_ts REAL,             -- for one-shot re-check of concentration rejects
    updated_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_state       ON tokens(state);
CREATE INDEX IF NOT EXISTS idx_tokens_next_check   ON tokens(next_check_ts);

-- Every enrichment snapshot, kept for backtesting the scoring model.
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mint            TEXT NOT NULL REFERENCES tokens(mint) ON DELETE CASCADE,
    ts              REAL NOT NULL,
    -- security facts
    mint_revoked    INTEGER,
    freeze_revoked  INTEGER,
    lp_secured      INTEGER,
    lp_lock_days    REAL,
    top10_pct       REAL,
    dev_pct         REAL,
    metadata_mutable INTEGER,
    bundled_snipers INTEGER,
    rugcheck_score  REAL,
    rugcheck_label  TEXT,
    -- market facts
    price_usd       REAL,
    liquidity_usd   REAL,
    market_cap_usd  REAL,
    fdv_usd         REAL,
    vol24h_usd      REAL,
    vol_liq_ratio   REAL,
    buys_1h         INTEGER,
    sells_1h        INTEGER,
    buys_sells_ratio REAL,
    holders         INTEGER,
    holder_growth_1h REAL,
    price_change_1h REAL,
    age_minutes     REAL,
    -- social facts
    has_socials     INTEGER,
    paid_profile    INTEGER,
    active_meta     INTEGER,
    -- verdict
    passed_hard     INTEGER,
    score           REAL,
    score_security  REAL,
    score_traction  REAL,
    score_social    REAL,
    score_momentum  REAL,
    raw_json        TEXT               -- full snapshot for debugging
);

CREATE INDEX IF NOT EXISTS idx_snapshots_mint ON snapshots(mint, ts);

-- One row per alert dispatched.
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mint            TEXT NOT NULL REFERENCES tokens(mint) ON DELETE CASCADE,
    ts              REAL NOT NULL,
    score           REAL NOT NULL,
    is_followup     INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,              -- e.g. "threshold" / "score +18" / "LP pulled"
    price_usd       REAL,
    market_cap_usd  REAL,
    liquidity_usd   REAL,
    snapshot_id     INTEGER REFERENCES snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_alerts_mint ON alerts(mint, ts);

-- Outcome tracking: price at +1h/+6h/+24h after each alert for model tuning.
CREATE TABLE IF NOT EXISTS outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id        INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    mint            TEXT NOT NULL,
    checkpoint_h    INTEGER NOT NULL,  -- 1, 6, 24
    due_ts          REAL NOT NULL,
    recorded_ts     REAL,
    price_usd       REAL,
    liquidity_usd   REAL,
    multiple        REAL,              -- price / entry price
    rugged          INTEGER,           -- 1 if liquidity collapsed
    UNIQUE(alert_id, checkpoint_h)
);

CREATE INDEX IF NOT EXISTS idx_outcomes_due ON outcomes(recorded_ts, due_ts);

-- Simple key/value for bot meta (last heartbeat, last daily summary, etc.)
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
