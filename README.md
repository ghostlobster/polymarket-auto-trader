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

## Risk Warning

Prediction market trading involves real financial risk. Start with `DRY_RUN=true` and small position sizes. Past performance does not guarantee future results.
