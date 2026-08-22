"""アプリケーション設定（.envファイルから環境変数を読み込み）"""
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    # ブローカー接続（moomoo OpenDゲートウェイ）
    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111
    moomoo_trade_password_md5: str = ""
    moomoo_trade_env: str = "SIMULATE"  # SIMULATE=模擬取引, REAL=本番取引
    # 発注・口座照会時の acc_id。0 = 先頭口座（get_acc_list()結果の1つ目）を使用。
    # 明示指定する場合は scripts/check_acc_list.py の結果から該当 acc_id を控えて .env に設定する。
    moomoo_acc_id: int = 0
    # JP口座区分（SubAccType）。JP_GENERAL=一般 / JP_TOKUTEI=特定 / JP_NISA_GENERAL=一般NISA 等。
    # place_order() に jp_acc_type として渡される。
    # 公式ドキュメント: https://openapi.moomoo.com/moomoo-api-doc/en/trade/trade.html
    # デフォルトは JP_TOKUTEI（特定口座・源泉徴収あり）。税務処理が自動化される安全側の既定値。
    moomoo_jp_acc_type: str = "JP_TOKUTEI"

    # 株価データ取得元。"yfinance"（既定）or "moomoo"。
    # 既定を yfinance にしているのは、moomoo 経路は OpenD 未起動時に空の
    # DataFrame を返すだけで例外にならず、price_cache が更新されないまま
    # 古い価格でシグナルが出るため。moomoo を使う環境では .env で明示的に
    # DATA_SOURCE=moomoo を指定すること。
    # moomoo は履歴K線取得にクォータ制限があるため、切替前/デプロイ直後は
    # scripts/fetch_test_moomoo.py の get_history_kl_quota() で残量を確認すること。
    data_source: str = "yfinance"

    # トレーディング基本設定
    dry_run: bool = True                    # True: 注文を実際には送信しない
    max_positions: int = 3                  # 同時保有ポジション数の上限
    risk_per_trade_pct: float = 0.01        # 1トレードあたりのリスク（資産の1%）
    max_portfolio_exposure_pct: float = 0.90  # ポートフォリオ全体のエクスポージャー上限（90%）
    daily_loss_limit_pct: float = 0.03      # 日次損失上限（3%超で新規エントリー停止）

    # 自動売買のポジション数カウントから除外するティッカー（キャンペーン取得株など、
    # 保有しているがシステム管理下ではない銘柄）。
    # ただし、システム自身が新規エントリーして trade_log に OPEN レコードを持つ場合は
    # 通常通りカウントされ、重複買い増しを防ぐ。
    # .env では カンマ区切りで指定: IGNORED_TICKERS=PAYP,NVDA,MSFT
    # NoDecode: pydantic-settings の自動 JSON パースを抑止し、field_validator で CSV を解析する
    ignored_tickers: Annotated[list[str], NoDecode] = []

    @field_validator("ignored_tickers", mode="before")
    @classmethod
    def _parse_ignored_tickers(cls, v):
        if isinstance(v, str):
            return [t.strip().upper() for t in v.split(",") if t.strip()]
        return v

    # データベース接続
    database_url: str = "postgresql://autotrd:password@localhost:5432/autotrd"

    # Slack Webhook通知
    slack_webhook_url: str = ""  # Incoming Webhook URL

    # タイムゾーン
    tz: str = "Asia/Tokyo"

    # 外部API (Wikipedia等) アクセス時の User-Agent に含める連絡先
    # Wikimedia User-Agent policy: https://meta.wikimedia.org/wiki/User-Agent_policy
    contact_email: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# アプリ全体で使うシングルトン設定インスタンス
settings = Settings()
