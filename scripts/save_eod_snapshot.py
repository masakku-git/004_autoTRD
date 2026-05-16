#!/usr/bin/env python3
"""US 市場クローズ後（JST 早朝）に直近 US 営業日の EOD ポートフォリオを保存。

main.py は JST 13:00 頃に動いており、その時点で moomoo API が返すのは前 US 営業日の
クローズ値（市場が閉じているため）。一方 moomoo 画面のチャートはその日 (US 日付) の
クローズを表示するため、main.py が記録するスナップショットは日付ラベルが1営業日ずれる。

本スクリプトは取引ロジックから独立した EOD 記録専用で、JST 土曜 07:00 に動かす想定。
JST 07:00 は EST 17:00 (= 金曜 US 市場クローズ後 1時間) のため、直近金曜の確定値を取れる。
date ラベルは US 営業日基準 (== 直前の平日) で保存し、ダッシュボード表示と moomoo 画面の
日付がそろう。

使い方:
    python3 scripts/save_eod_snapshot.py

VPS cron 例 (JST 土曜 07:00):
    0 7 * * 6  cd ~/autoTRD && python3 scripts/save_eod_snapshot.py >> logs/eod_snapshot.log 2>&1
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

# プロジェクトルートを sys.path に追加（cron から直接呼ぶ場合に必要）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from src.broker.account import get_account_info  # noqa: E402
from src.models.base import get_session  # noqa: E402
from src.models.portfolio import PortfolioSnapshot  # noqa: E402
from src.utils.helpers import today_jst  # noqa: E402
from src.utils.logger import logger  # noqa: E402


def previous_us_trading_date(d: date | None = None) -> date:
    """JST 当日基準で「直近の US 営業日」を返す（土日スキップ）。

    JST 早朝 (07:00) に呼ばれる前提の挙動:
      JST 土 → 金 close を取りに行く → return 金
      JST 月 → 金 close (土日は休場) → return 前週金
      JST 火 → 月 close → return 月
    """
    d = d or today_jst()
    target = d - timedelta(days=1)
    while target.weekday() >= 5:  # Sat=5, Sun=6
        target -= timedelta(days=1)
    return target


def save_eod_snapshot() -> None:
    account = get_account_info()
    target = previous_us_trading_date()

    with get_session() as session:
        existing = session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.date == target)
        ).scalar_one_or_none()

        if existing:
            existing.total_equity = account.total_equity
            existing.cash = account.cash
            existing.positions_json = account.positions
            existing.num_positions = len(account.positions)
            action = "updated"
        else:
            session.add(
                PortfolioSnapshot(
                    date=target,
                    total_equity=account.total_equity,
                    cash=account.cash,
                    positions_json=account.positions,
                    num_positions=len(account.positions),
                )
            )
            action = "inserted"
        session.commit()

    logger.info(
        f"EOD snapshot {action}: date={target} equity=${account.total_equity:.2f} "
        f"cash=${account.cash:.2f} positions={len(account.positions)}"
    )


if __name__ == "__main__":
    save_eod_snapshot()
