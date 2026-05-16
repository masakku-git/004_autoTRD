#!/usr/bin/env python3
"""直近 N 件の portfolio_snapshots を出力（手動確認用）。

使い方:
    python3 scripts/inspect_recent_snapshots.py        # 直近5件
    python3 scripts/inspect_recent_snapshots.py 10     # 直近10件
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from src.models.base import get_session  # noqa: E402
from src.models.portfolio import PortfolioSnapshot  # noqa: E402


def main(n: int = 5) -> None:
    with get_session() as s:
        rows = (
            s.execute(
                select(PortfolioSnapshot)
                .order_by(PortfolioSnapshot.date.desc())
                .limit(n)
            )
            .scalars()
            .all()
        )
        print(f"{'date':<12} {'total_equity':>14} {'cash':>10} {'num_positions':>14}")
        print("-" * 55)
        for r in rows:
            print(
                f"{str(r.date):<12} "
                f"{r.total_equity:>14.2f} "
                f"{r.cash:>10.2f} "
                f"{r.num_positions:>14}"
            )


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    main(n)
