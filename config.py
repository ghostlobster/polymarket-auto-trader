from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # API keys
    anthropic_api_key: str
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_private_key: str = ""
    polymarket_chain_id: int = 137

    # Trading parameters
    max_position_usdc: float = 50.0
    max_concurrent_positions: int = 5
    min_edge_threshold: float = 0.05
    min_confidence_threshold: float = 0.6
    kelly_fraction: float = 0.25
    stop_loss_pct: float = 0.30
    take_profit_pct: float = 0.80
    scan_interval_minutes: int = 15

    # Operational
    dry_run: bool = False
    db_path: str = "trading.db"
    log_level: str = "INFO"

    # ------------------------------------------------------------------ #
    #  Cognitive-arbitrage engine                                        #
    # ------------------------------------------------------------------ #

    # Edge mode controls which external adapters are active.
    #   strict — Polymarket CLOB/Data API + DuckDuckGo only.
    #   hybrid — selective external augmentation (recommended default
    #            wires news + cross-market parity, leaves polling off).
    #   full   — every adapter on (subject to its own per-source flag).
    edge_mode: str = "hybrid"

    # Per-source feature flags. Each defaults OFF so absent keys don't crash;
    # the chosen edge_mode flips reasonable defaults via Settings.resolve_sources.
    newsapi_enabled: bool = False
    newsapi_key: str = ""
    gdelt_enabled: bool = False
    manifold_enabled: bool = False
    kalshi_enabled: bool = False
    metaculus_enabled: bool = False
    polling_aggregator_enabled: bool = False

    # Calibration auditor
    calibration_interval_secs: int = 3600
    calibration_min_resolutions_for_shrinkage: int = 20

    # Deterministic risk guardrails
    risk_cluster_cap_frac: float = 0.30  # ≤30% of bankroll across one cluster
    risk_category_cap_frac: float = 0.40  # ≤40% per category
    risk_resolution_window_cap_frac: float = 0.20  # ≤20% in any 24h resolution window
    risk_per_trade_cap_frac: float = 0.10  # ≤10% of bankroll on a single trade
    risk_min_trade_usdc: float = 5.0
    risk_shrinkage_floor: float = 0.25  # never shrink edge below 25% of raw

    # Order-book snapshotter
    snapshot_interval_secs: int = 300
    snapshot_market_limit: int = 40  # cap of distinct markets per pass

    # Theta gating in portfolio monitor
    theta_take_profit_pct: float = 0.40  # force TP when within window and up >40%
    theta_window_hours: float = 24.0  # apply theta gate inside this window
    theta_force_close_hours: float = 2.0  # always close < this many hours from resolution

    # Slippage / adverse-selection budget on thesis fills
    thesis_max_slippage: float = 0.02  # 2¢ vs. quoted mid before rejecting
    thesis_postfill_drift_track_min: int = 120  # log fill drift up to 2h after

    # Ensemble signal generator
    ensemble_enabled: bool = True
    ensemble_weight_prior: float = 0.25
    ensemble_weight_bull: float = 0.25
    ensemble_weight_bear: float = 0.25
    ensemble_weight_market: float = 0.25
    ensemble_disagreement_floor: float = 0.05

    # --- Copy-trading subsystem ---
    copy_enabled: bool = False
    copy_default_preset: str = "scaled_market"
    copy_poll_seconds: int = 30
    copy_audit_interval_secs: int = 60
    copy_audit_window_secs: int = 120
    copy_min_confirmed_paper_trades: int = 20
    copy_audit_miss_rate_demote: float = 0.10
    copy_report_refresh_throttle_secs: int = 30
    copy_paper_starting_usdc: float = 1000.0

    # Discovery
    leaderboard_refresh_hours: int = 24
    leader_min_trades: int = 50
    leader_min_weeks_profit_frac: float = 0.6
    leader_min_wallet_volume: float = 20000.0
    leader_max_resolution_sniper_frac: float = 0.8
    leaderboard_top_n_for_llm: int = 30
    leaderboard_keep_n: int = 10

    # Web UI
    copy_web_enabled: bool = False
    copy_web_host: str = "127.0.0.1"
    copy_web_port: int = 8765

    # OAuth (web UI login)
    oauth_session_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    oauth_allowed_emails: str = ""
    oauth_base_url: str = "http://127.0.0.1:8765"

    # Polymarket data API (separate from CLOB)
    polymarket_data_api: str = "https://data-api.polymarket.com"

    # External-source endpoints (free public APIs)
    manifold_api_base: str = "https://api.manifold.markets/v0"
    metaculus_api_base: str = "https://www.metaculus.com/api2"
    kalshi_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    newsapi_base: str = "https://newsapi.org/v2"
    gdelt_base: str = "https://api.gdeltproject.org/api/v2/doc/doc"
    polling_aggregator_base: str = "https://projects.fivethirtyeight.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def resolve_sources(self) -> dict[str, bool]:
        """
        Effective per-source enablement given `edge_mode` + individual flags.

        Per-source flags can override modes upward (force-enable a source while
        in `strict`) or downward (force-disable a source while in `full`). Mode
        sets the default; an explicit flag wins.
        """
        mode = (self.edge_mode or "hybrid").lower()
        if mode == "strict":
            defaults = {
                "news": False,
                "gdelt": False,
                "manifold": False,
                "kalshi": False,
                "metaculus": False,
                "polling": False,
            }
        elif mode == "full":
            defaults = {
                "news": True,
                "gdelt": True,
                "manifold": True,
                "kalshi": True,
                "metaculus": True,
                "polling": True,
            }
        else:  # hybrid
            defaults = {
                "news": True,
                "gdelt": False,
                "manifold": True,
                "kalshi": False,
                "metaculus": True,
                "polling": False,
            }
        return {
            "news": self.newsapi_enabled or defaults["news"],
            "gdelt": self.gdelt_enabled or defaults["gdelt"],
            "manifold": self.manifold_enabled or defaults["manifold"],
            "kalshi": self.kalshi_enabled or defaults["kalshi"],
            "metaculus": self.metaculus_enabled or defaults["metaculus"],
            "polling": self.polling_aggregator_enabled or defaults["polling"],
        }
