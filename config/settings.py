"""アプリケーション設定（.envファイルから環境変数を読み込み）"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ブローカー接続（moomoo OpenDゲートウェイ）
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111
    moomoo_trade_password_md5: str = ""
    moomoo_trade_env: str = "SIMULATE"  # SIMULATE=模擬取引, REAL=本番取引

    # トレーディング基本設定
    dry_run: bool = True                    # True: 注文を実際には送信しない
    max_positions: int = 3                  # 同時保有ポジション数の上限
    risk_per_trade_pct: float = 0.02        # 1トレードあたりのリスク（資産の2%）
    max_portfolio_exposure_pct: float = 0.90  # ポートフォリオ全体のエクスポージャー上限（90%）
    daily_loss_limit_pct: float = 0.03      # 日次損失上限（3%超で新規エントリー停止）

    # データベース接続
    database_url: str = "postgresql://autotrd:password@localhost:5432/autotrd"

    # Slack Webhook通知
    slack_webhook_url: str = ""  # Incoming Webhook URL

    # タイムゾーン
    tz: str = "Asia/Tokyo"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# アプリ全体で使うシングルトン設定インスタンス
settings = Settings()
