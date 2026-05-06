# Polymarket Auto-Trader

A multi-agent AI system for automated trading on [Polymarket](https://polymarket.com) prediction markets.

## Architecture

Seven Claude-powered agents collaborate each trading cycle:

```
Orchestrator
├── Market Scanner    → finds tradeable markets across all categories
├── Research Analyst  → web-searches to assess true outcome probability
├── Signal Generator  → produces YES/NO signals with edge & confidence
├── Risk Manager      → Kelly Criterion position sizing + hard limits
├── Order Executor    → places limit/market orders via Polymarket CLOB
└── Portfolio Monitor → stop-losses, take-profits, P&L tracking
```

All agents use prompt caching for efficiency. State is persisted in SQLite.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

### 3. Get Polymarket API credentials

You need a funded Polygon wallet connected to Polymarket. Then derive API credentials:

```bash
python -m polymarket.auth setup
```

Paste the output into your `.env` file.

### 4. Set your Anthropic API key

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
# Live trading
python main.py

# Dry-run (no real orders)
DRY_RUN=true python main.py
```

The system runs a full trading cycle every 15 minutes by default (`SCAN_INTERVAL_MINUTES`).

## Configuration

Key settings in `.env`:

| Variable | Default | Description |
|---|---|---|
| `MAX_POSITION_USDC` | 50.0 | Max USDC per trade |
| `MAX_CONCURRENT_POSITIONS` | 5 | Max open positions at once |
| `MIN_EDGE_THRESHOLD` | 0.05 | Minimum price edge to trade |
| `MIN_CONFIDENCE_THRESHOLD` | 0.6 | Minimum research confidence |
| `KELLY_FRACTION` | 0.25 | Fraction of Kelly Criterion to apply |
| `STOP_LOSS_PCT` | 0.30 | Exit position if down 30% |
| `TAKE_PROFIT_PCT` | 0.80 | Exit position if up 80% |
| `DRY_RUN` | false | Simulate without placing orders |

## Testing

```bash
pip install pytest pytest-asyncio
pytest tests/
```

## Database

State is stored in `trading.db` (SQLite). Inspect it:

```bash
# Recent signals
sqlite3 trading.db "SELECT question, side, edge, confidence FROM signals ORDER BY created_at DESC LIMIT 10;"

# P&L history
sqlite3 trading.db "SELECT snapshot_at, realized_pnl, unrealized_pnl FROM pnl_snapshots ORDER BY snapshot_at DESC LIMIT 20;"

# Open positions
sqlite3 trading.db "SELECT question, side, avg_price, current_price, unrealized_pnl FROM positions WHERE closed_at IS NULL;"
```

## Copy-trading subsystem (parallel mode)

The thesis pipeline above runs every 15 minutes. A separate, time-sensitive
copy-trading subsystem can run in parallel: it discovers consistent
top-performing Polymarket wallets, polls their trades on a tight cadence,
and copies them through one of five strategy presets (`mirror`, `scaled_market`,
`scaled_limit`, `conservative`, `shadow`).

Lifecycle: `discovered → shadow → paper → live`. Promotion to `live` is gated
on confirmed paper-mode profit and a low audit-miss rate; never automatic.

Enable:

```bash
COPY_ENABLED=true COPY_WEB_ENABLED=true python main.py
# Then visit http://127.0.0.1:8765/profiles
```

The web UI lists every tracked trader with PnL, hit rate, and audit alerts.
Reports refresh on every page load (throttled to 30s) so you don't need a cron.

Backtest a single wallet against historical data:

```bash
python -m copytrader.evaluate --wallet 0x... --since 30d --capital 1000 --preset scaled_market
```

See `.env.example` for all `COPY_*` knobs.

## Risk Warning

Prediction market trading involves real financial risk. Start with `DRY_RUN=true` and small position sizes. Past performance does not guarantee future results.
