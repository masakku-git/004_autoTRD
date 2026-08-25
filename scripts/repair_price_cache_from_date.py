#!/usr/bin/env python3
"""指定日以降の price_cache を削除し、確定値で取り直すメンテナンススクリプト。

未確定（場中）の日足が price_cache に焼き付いたときの修復用。
2026-08-25 00:39 JST の手動再実行で、米国市場が 2026-08-24 の場中（11:39 ET）
だったにもかかわらず「前日まで」＝ 8/24 として取得したため、全50銘柄の 8/24 の
足が場中スナップショットのまま保存された（実測で TSLA +2.07%、WMT -1.62% 等）。
fetcher 側は last_completed_us_session() によるクランプと最新日の上書きで
再発防止・自己修復するようにしたが、既に焼き付いた行は本スクリプトで掃除する。

「指定日以降」をまとめて消すのは、途中の日付だけ消すと get_last_cached_date が
それより新しい日付を返し、差分取得の対象から外れて穴が埋まらないため。

使い方:
    # 確認のみ（削除しない）
    .venv/bin/python scripts/repair_price_cache_from_date.py --from-date 2026-08-24

    # 削除して再取得まで行う
    .venv/bin/python scripts/repair_price_cache_from_date.py --from-date 2026-08-24 --apply

    # 銘柄を絞る場合
    .venv/bin/python scripts/repair_price_cache_from_date.py --from-date 2026-08-24 \\
        --tickers TSLA,WMT --apply

注意:
    再取得は settings.data_source（moomoo/yfinance）に従う。moomoo 経路では
    銘柄数ぶんの履歴K線クォータを消費するため、残量に注意すること。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from src.data.fetcher import last_completed_us_session, update_price_cache_batch  # noqa: E402
from src.models.base import get_session  # noqa: E402
from src.models.price import PriceCache  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="指定日以降の price_cache を削除して取り直す"
    )
    parser.add_argument(
        "--from-date",
        required=True,
        type=date.fromisoformat,
        help="この日付以降（当日を含む）の行を削除対象にする（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--tickers",
        default="",
        help="対象銘柄をカンマ区切りで指定（省略時は該当する全銘柄）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="削除と再取得を実行（省略時は確認のみ）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    session_date = last_completed_us_session()
    if args.from_date > session_date:
        print(
            f"⚠ 指定日 {args.from_date} は直近の確定セッション {session_date} より"
            "後です。削除しても確定値を取り直せません。"
        )
        return

    with get_session() as session:
        query = select(PriceCache).where(PriceCache.date >= args.from_date)
        if tickers:
            query = query.where(PriceCache.ticker.in_(tickers))
        rows = session.execute(
            query.order_by(PriceCache.ticker, PriceCache.date)
        ).scalars().all()

        if not rows:
            print(f"{args.from_date} 以降の行は見つかりませんでした。対応は不要です。")
            return

        affected = sorted({r.ticker for r in rows})
        by_date: dict[date, int] = {}
        for r in rows:
            by_date[r.date] = by_date.get(r.date, 0) + 1

        print(f"{args.from_date} 以降の行が {len(rows)} 件見つかりました "
              f"（{len(affected)}銘柄）:")
        for d in sorted(by_date):
            print(f"  {d}  {by_date[d]}件")
        print(f"  対象銘柄: {', '.join(affected)}")

        if not args.apply:
            print("\n※ 確認モードのため削除していません。")
            print("   実行するには --apply を付けて再実行してください。")
            return

        for r in rows:
            session.delete(r)
        session.commit()
        print(f"\n✅ {len(rows)} 件を削除しました。")

    print(f"再取得します（{len(affected)}銘柄、確定セッション {session_date} まで）...")
    results = update_price_cache_batch(affected)
    total = sum(results.values())
    print(f"✅ 再取得完了: {total} 行を書き込みました。")
    empty = [t for t, n in results.items() if n == 0]
    if empty:
        print(f"⚠ 書き込み0件だった銘柄: {', '.join(empty)}")
        print("  取得失敗の可能性があります。ログを確認してください。")


if __name__ == "__main__":
    main()
