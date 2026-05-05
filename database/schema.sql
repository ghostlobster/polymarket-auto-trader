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
    created_at TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(closed_at);
CREATE INDEX IF NOT EXISTS idx_messages_processed ON agent_messages(processed, created_at);
