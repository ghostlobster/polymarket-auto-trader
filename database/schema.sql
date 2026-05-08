CREATE TABLE IF NOT EXISTS markets_scanned (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT,
    volume REAL,
    spread REAL,
    opportunity_score REAL,
    scanned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    strength TEXT NOT NULL,
    estimated_probability REAL NOT NULL,
    market_price REAL NOT NULL,
    edge REAL NOT NULL,
    rationale TEXT,
    research_summary TEXT,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT DEFAULT 'thesis',
    leader_wallet TEXT DEFAULT '',
    preset TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    signal_id TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size_usdc REAL NOT NULL,
    price REAL NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    placed_at TEXT,
    filled_at TEXT,
    fill_price REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    question TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    avg_price REAL NOT NULL,
    current_price REAL,
    unrealized_pnl REAL,
    realized_pnl REAL DEFAULT 0,
    opened_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    processed INTEGER DEFAULT 0,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_usdc REAL NOT NULL,
    available_usdc REAL,
    open_positions INTEGER,
    realized_pnl REAL,
    unrealized_pnl REAL,
    snapshot_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id);
CREATE INDEX IF NOT EXISTS idx_signals_source ON signals(source);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_at);
CREATE INDEX IF NOT EXISTS idx_messages_processed ON agent_messages(processed, created_at);

-- ----- Copy-trading subsystem -----

CREATE TABLE IF NOT EXISTS tracked_traders (
    wallet TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'discovered',     -- discovered|shadow|paper|live|disabled
    preset TEXT NOT NULL DEFAULT 'scaled_market',
    score REAL DEFAULT 0,
    sample_size INTEGER DEFAULT 0,
    weeks_profitable_frac REAL DEFAULT 0,
    max_drawdown REAL DEFAULT 0,
    total_volume_usdc REAL DEFAULT 0,
    resolution_sniper_frac REAL DEFAULT 0,
    last_seen_ts INTEGER DEFAULT 0,
    last_evaluated_at TEXT,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leader_trades (
    wallet TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,                            -- BUY|SELL
    outcome TEXT NOT NULL,                         -- YES|NO
    size_usdc REAL NOT NULL,
    price REAL NOT NULL,
    observed_at TEXT NOT NULL,
    expected_copy INTEGER NOT NULL DEFAULT 0,      -- 1 if we should have copied
    copy_order_id TEXT DEFAULT '',
    copy_mode TEXT DEFAULT '',                     -- shadow|paper|live
    skip_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (wallet, tx_hash)
);
CREATE INDEX IF NOT EXISTS idx_leader_trades_wallet_obs ON leader_trades(wallet, observed_at);
CREATE INDEX IF NOT EXISTS idx_leader_trades_expected ON leader_trades(expected_copy, copy_order_id);

CREATE TABLE IF NOT EXISTS paper_orders (
    id TEXT PRIMARY KEY,
    wallet TEXT NOT NULL,                          -- the leader being followed
    market_id TEXT NOT NULL,
    signal_id TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size_usdc REAL NOT NULL,
    price REAL NOT NULL,                           -- simulated fill VWAP
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    placed_at TEXT,
    filled_at TEXT,
    fill_price REAL,
    error TEXT,
    leader_tx_hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_paper_orders_wallet ON paper_orders(wallet);

CREATE TABLE IF NOT EXISTS paper_positions (
    id TEXT PRIMARY KEY,
    wallet TEXT NOT NULL,
    market_id TEXT NOT NULL,
    question TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,                            -- YES|NO
    size REAL NOT NULL,
    avg_price REAL NOT NULL,
    current_price REAL,
    unrealized_pnl REAL,
    realized_pnl REAL DEFAULT 0,
    opened_at TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_wallet_open ON paper_positions(wallet, closed_at);

CREATE TABLE IF NOT EXISTS copy_performance (
    wallet TEXT NOT NULL,
    mode TEXT NOT NULL,                            -- shadow|paper|live|backtest
    trades_observed INTEGER DEFAULT 0,
    trades_copied INTEGER DEFAULT 0,
    copy_hit_rate REAL DEFAULT 0,
    audit_miss_rate REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    last_updated TEXT NOT NULL,
    PRIMARY KEY (wallet, mode)
);

CREATE TABLE IF NOT EXISTS audit_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    leader_tx_hash TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_alerts_wallet ON audit_alerts(wallet, created_at);

-- ----- Web UI auth -----

CREATE TABLE IF NOT EXISTS web_users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    email       TEXT,
    name        TEXT,
    avatar_url  TEXT,
    is_allowed  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    last_login  TEXT NOT NULL,
    UNIQUE (provider, provider_id)
);
CREATE INDEX IF NOT EXISTS idx_web_users_email ON web_users(email);
