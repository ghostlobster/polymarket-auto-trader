# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run the trader
python main.py                         # live trading
DRY_RUN=true python main.py            # dry-run (no real orders)
COPY_ENABLED=true COPY_WEB_ENABLED=true python main.py  # with copy-trading + web UI

# Tests
pytest tests/                          # all tests
pytest tests/test_signal_generator.py  # single test file
pytest tests/ -k "test_paper"          # run matching tests

# Lint / type check
ruff check .
ruff format .
pyright

# Backtest a wallet
python -m copytrader.evaluate --wallet 0x... --since 30d --capital 1000 --preset scaled_market

# Get Polymarket API credentials
python -m polymarket.auth setup

# Inspect the database
sqlite3 trading.db "SELECT question, side, edge, confidence FROM signals ORDER BY created_at DESC LIMIT 10;"
sqlite3 trading.db "SELECT * FROM tracked_traders ORDER BY score DESC;"
```

## Architecture

### Two parallel trading pipelines

`main.py` runs asyncio tasks for two independent pipelines:

**Thesis pipeline** (15-min cadence) — research-driven, LLM-heavy:
```
OrchestratorAgent.run_cycle()
  ├── PortfolioMonitorAgent.check()   → stop-losses, take-profits, P&L snapshots
  ├── MarketScannerAgent.scan()       → fetches active markets, scores opportunities
  ├── ResearchAnalystAgent.analyze()  → web search to assess true probability
  ├── SignalGeneratorAgent.generate() → produces Signal with edge/confidence
  ├── RiskManagerAgent.assess()       → Kelly Criterion sizing, hard limits
  └── OrderExecutorAgent.execute()    → places orders via PolymarketClient
```

**Copy-trading pipeline** (runs in parallel when `COPY_ENABLED=true`):
```
TraderDiscoveryAgent.discover()  (24h cadence) → leaderboard → LLM scoring → upsert tracked_traders
CopyTraderAgent.cycle()          (30s cadence) → poll leader trades → apply strategy preset → CopyExecutor
CopyAuditAgent.cycle()           (60s cadence) → reconcile expected vs actual copies, demote on miss rate
web/server.py FastAPI            (on-demand)   → profile review UI at /profiles
```

### Agent base class

All LLM-using agents subclass `BaseAgent` (`agents/base.py`). It manages:
- The Anthropic tool-use loop (up to `max_tool_rounds` turns)
- **Ephemeral prompt caching** on the system prompt — every agent does this automatically
- Tool dispatch via a `handlers: dict[str, async callable]` map
- Subclasses set `self.system_prompt`, `self.model`, `self.tools`, `self.handlers`

`MODEL_SONNET = "claude-sonnet-4-6"` and `MODEL_OPUS = "claude-opus-4-7"` are defined on `BaseAgent`.

### Tools architecture

Tools in `tools/` are built as paired `(schema_list, handler_map)` tuples, then passed to `BaseAgent.__init__`. The handlers receive the `block.input` dict and return a JSON string. `tools/market_tools.py` covers the CLOB API; `tools/web_tools.py` covers DuckDuckGo search; `tools/db_tools.py` covers database reads.

`OrchestratorAgent` does **not** use the LLM — it orchestrates deterministically.

### Polymarket client

`PolymarketClient` (`polymarket/client.py`) wraps the synchronous `py-clob-client` using `asyncio.run_in_executor`. All prices are normalised to `0.0–1.0`. A separate `PolymarketDataClient` (`polymarket/data_client.py`) hits the public data API for leaderboards and wallet activity used by the copy-trading subsystem.

### Database

SQLite via `aiosqlite` with WAL mode. The schema (`database/schema.sql`) is applied idempotently on startup. Additive column migrations are listed in `database/db.py:_MIGRATIONS` as `(table, column, ddl)` tuples — append here rather than editing the schema for backwards-compatible changes.

Two separate table sets exist: `orders`/`positions` for live trades, and `paper_orders`/`paper_positions` (keyed by `wallet`) for copy-trading simulation.

### Copy-trading lifecycle

Trader status flows: `discovered → shadow → paper → live → disabled`

- **shadow**: logs only, no execution, `expected_copy=False`
- **paper**: `PaperBroker` simulates fills by walking the live order book; one broker instance per leader wallet keyed in `CopyExecutor._brokers`
- **live**: real orders via `PolymarketClient`; also gated by `DRY_RUN`

Promotion from paper→live requires: ≥20 confirmed paper trades, positive paper PnL, and audit miss rate below threshold. Promotion is manual via the web UI — never automatic.

### Copy strategy presets

Five presets in `copytrader/strategies.py` encode order type, notional scale, max age, slippage tolerance, and minimum time-to-resolution:

| preset | order_type | scale | max_age | max_slippage | min_resolves_in |
|---|---|---|---|---|---|
| mirror | market | 1.0x | 60s | 5% | any |
| scaled_market | market | 1% | 120s | 3% | 24h |
| scaled_limit | limit | 1% | 120s | 1% | 24h |
| conservative | limit | 0.5% | 90s | 1% | 7d |
| shadow | none | 0 | — | — | — |

### Signal model

`Signal.is_actionable` returns `True` when `abs(edge) >= 0.05 and confidence >= 0.6`. Edge is `estimated_probability - market_price`. `Signal.source` is `'thesis'` for research-pipeline signals or `'copy'` for copy-trading signals (which also carry `leader_wallet` and `preset`).

## Configuration

All settings live in `config.py` as a Pydantic `Settings` class, loaded from `.env`. Copy `.env.example` to `.env` to start. Key knobs:

- `DRY_RUN=true` — safe to test without capital risk
- `COPY_ENABLED=true` — activates the copy-trading pipeline
- `COPY_WEB_ENABLED=true` — starts FastAPI on `COPY_WEB_HOST:COPY_WEB_PORT` (default `127.0.0.1:8765`)
- OAuth (`GOOGLE_CLIENT_ID`, `GITHUB_CLIENT_ID`, `OAUTH_ALLOWED_EMAILS`) gates the web UI; without these set, `oauth_session_secret` defaults to a dev value

## Testing

Tests use `pytest-asyncio` in `asyncio_mode = "auto"` (configured in `pyproject.toml`). No live API calls are made in tests — all Polymarket and Anthropic interactions are mocked. `ruff` enforces line length 100 and import order; `pyright` runs in basic mode targeting Python 3.11.
