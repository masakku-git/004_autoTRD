"""moomoo APIから株価データを取得し、既存yfinanceキャッシュと比較する動作確認スクリプト。

DATA_SOURCE=moomoo への切替前/切替直後に、moomoo由来のOHLCVが既存の
price_cache（yfinance由来）と大きく乖離していないか、履歴K線クォータに
余裕があるかを目視確認するために使う。

使い方:
    python scripts/fetch_test_moomoo.py
    python scripts/fetch_test_moomoo.py --tickers AAPL,MSFT,NVDA --days 30

前提:
    - OpenDが起動していること
    - .env に moomoo_host / moomoo_port が正しく設定されていること
    - 本スクリプトは読み取り専用（price_cacheへの書き込みは行わない）
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.fetcher import (
    PriceFetchError,
    fetch_from_moomoo,
    get_ohlcv,
    log_history_kl_quota,
    moomoo_quote_ctx,
)

DEFAULT_TICKERS = ["AAPL", "MSFT", "NVDA"]


def _compare_ticker(ticker: str, days: int, ctx) -> None:
    start = date.today() - timedelta(days=days)

    print(f"\n{'=' * 70}")
    print(f"  {ticker}  ({start} 〜 {date.today()})")
    print("=" * 70)

    moomoo_df = fetch_from_moomoo(ticker, start, ctx=ctx)
    if moomoo_df.empty:
        print("  [moomoo] データなし/取得失敗")
        return

    yfinance_df = get_ohlcv(ticker, start=start, ensure_updated=False)

    print(f"  [moomoo]   行数={len(moomoo_df)}  "
          f"直近Close={moomoo_df.iloc[-1]['Close']:.2f}  "
          f"直近日={moomoo_df.iloc[-1]['Date'].date()}")

    if yfinance_df.empty:
        print("  [yfinance] キャッシュなし（比較不可）")
        return

    print(f"  [cache]    行数={len(yfinance_df)}  "
          f"直近Close={yfinance_df.iloc[-1]['Close']:.2f}  "
          f"直近日={yfinance_df.index[-1].date()}")

    # 共通の日付でCloseを突き合わせる
    moomoo_by_date = moomoo_df.set_index("Date")["Close"]
    common_dates = moomoo_by_date.index.intersection(yfinance_df.index)
    if len(common_dates) == 0:
        print("  [diff]     共通する日付なし（比較不可）")
        return

    diffs = (moomoo_by_date.loc[common_dates] - yfinance_df.loc[common_dates, "Close"]).abs()
    pct_diffs = diffs / yfinance_df.loc[common_dates, "Close"]
    print(f"  [diff]     共通{len(common_dates)}日 / 最大乖離率={pct_diffs.max():.4%} "
          f"/ 平均乖離率={pct_diffs.mean():.4%}")


def main() -> None:
    parser = argparse.ArgumentParser(description="moomoo価格データの動作確認")
    parser.add_argument("--tickers", type=str, default=",".join(DEFAULT_TICKERS))
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    print("moomoo価格データ 動作確認スクリプト（読み取り専用、price_cacheへの書き込みなし）")

    # 本体と同じく OpenD 接続は1本だけ張って全銘柄で使い回す
    try:
        with moomoo_quote_ctx() as ctx:
            log_history_kl_quota(ctx)
            for ticker in tickers:
                _compare_ticker(ticker, args.days, ctx)
    except PriceFetchError as e:
        print(f"\n[エラー] moomoo OpenD に接続できません: {e}")
        print("OpenD が起動しているか、.env の moomoo_host / moomoo_port を確認してください。")
        sys.exit(1)

    print("\n完了")


if __name__ == "__main__":
    main()
