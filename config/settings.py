from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Broker (moomoo OpenD)
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111
    moomoo_trade_password_md5: str = ""
    moomoo_trade_env: str = "SIMULATE"  # SIMULATE or REAL

    # Trading
    dry_run: bool = True
    max_positions: int = 3
    risk_per_trade_pct: float = 0.02
    max_portfolio_exposure_pct: float = 0.90
    daily_loss_limit_pct: float = 0.03

    # Database (PostgreSQL)
    database_url: str = "postgresql://autotrd:password@localhost:5432/autotrd"

    # Notification
    line_notify_token: str = ""

    # Timezone
    tz: str = "Asia/Tokyo"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
