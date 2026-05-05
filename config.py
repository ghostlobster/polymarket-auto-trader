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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
