
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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
