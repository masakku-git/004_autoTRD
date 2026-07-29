#!/usr/bin/env python3
"""price_cache に永続化された NaN 行を確認・削除するメンテナンススクリプト。

2026-07-29 の障害で、yfinance の取得失敗により NaN が price_cache に
保存されたことがある。fetcher 側の修正で新規混入は防止済みだが、
過去に保存された NaN 行は差分更新では上書きされないため本スクリプトで掃除する。

使い方:
    python3 scripts/cleanup_nan_price_cache.py            # 確認のみ（削除しない）
    python3 scripts/cleanup_nan_price_cache.py --delete   # 削除を実行

削除後の挙動:
    - 最新日付の行を削除した場合は、次回の日次実行時に yfinance から自動で再取得される
    - 過去の中間日付の行を削除した場合はその日だけ欠損として扱われる（休場日と同じ扱いで、
      判定ロジックへの影響は軽微）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import or_, select  # noqa: E402

from src.models.base import get_session  # noqa: E402
from src.models.price import PriceCache  # noqa: E402

_NAN = float("nan")


def _nan_condition():
    # PostgreSQL では 'NaN'::float8 = 'NaN'::float8 が真のため等号で検出できる
    return or_(
        PriceCache.open == _NAN,
        PriceCache.high == _NAN,
        PriceCache.low == _NAN,
        PriceCache.close == _NAN,
        PriceCache.adj_close == _NAN,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="price_cache の NaN 行を確認・削除")
    parser.add_argument("--delete", action="store_true", help="削除を実行（省略時は確認のみ）")
    args = parser.parse_args()

    with get_session() as session:
        rows = session.execute(
            select(PriceCache)
            .where(_nan_condition())
            .order_by(PriceCache.ticker, PriceCache.date)
        ).scalars().all()

        if not rows:
            print("NaNを含む行は見つかりませんでした。対応は不要です。")
            return

        print(f"NaNを含む行が {len(rows)} 件見つかりました:")
        for r in rows:
            print(
                f"  {r.ticker:8} {r.date}  "
                f"open={r.open} high={r.high} low={r.low} "
                f"close={r.close} adj_close={r.adj_close}"
            )

        if not args.delete:
            print("\n※ 確認モードのため削除していません。")
            print("   削除するには --delete を付けて再実行してください:")
            print("   .venv/bin/python scripts/cleanup_nan_price_cache.py --delete")
            return

        for r in rows:
            session.delete(r)
        session.commit()
        print(f"\n✅ {len(rows)} 件を削除しました。")
        print("最新日付の行は次回の日次実行時に自動で再取得されます。")


if __name__ == "__main__":
    main()
