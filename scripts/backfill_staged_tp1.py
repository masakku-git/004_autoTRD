"""段階利確(TP1)の未計上分を trade_log にバックフィルする（一回限りのスクリプト）。

2026-07-25 の executor_v2 修正前は、TP1で一部だけ売却した分の損益が trade_log.pnl に
入らなかった（notes のみ）。該当の5件を、実約定価格で分割CLOSED行として追加する。

注意: 過去のログやメモにある +$158.24 は「発注時の推定価格」で計算した値で、実約定価格で
計算し直すと合計は約 +$107.74 になる（AMZN/INTC/MS/INTC/ABBV）。本スクリプトは実約定価格を使う。

使い方:
    python scripts/backfill_staged_tp1.py            # dry-run（DBは変更せず内容だけ表示）
    python scripts/backfill_staged_tp1.py --apply    # 実際に追加する

冪等: notes に BACKFILL_MARKER が入った行が既にあれば、その注文分は追加しない。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from src.models.base import get_session
from src.models.trade import Order, TradeLog
from src.utils.logger import logger

BACKFILL_MARKER = "段階利確バックフィル"


@dataclass(frozen=True)
class Partial:
    ticker: str
    sell_order_id: int     # 段階利確の売り注文（orders.id）
    entry_order_id: int    # 元ロットの買い注文（orders.id）
    quantity: int          # 段階利確で売った株数
    fill_override: float | None = None  # orders.filled_price が未反映の場合の実約定価格


# orders.csv（2026-09-19時点）と moomoo 履歴照会(2026-09-19)で突合済みの5件。
PARTIALS = [
    Partial("AMZN", sell_order_id=10, entry_order_id=6, quantity=2),
    Partial("INTC", sell_order_id=25, entry_order_id=21, quantity=3),
    Partial("MS", sell_order_id=80, entry_order_id=77, quantity=1),
    Partial("INTC", sell_order_id=84, entry_order_id=81, quantity=2),
    # 売り注文が SUBMITTED のまま filled_price 未反映。moomoo 履歴の dealt_avg_price=259.92
    Partial("ABBV", sell_order_id=94, entry_order_id=89, quantity=1, fill_override=259.92),
]


def backfill(apply: bool) -> float:
    total = 0.0
    with get_session() as session:
        for p in PARTIALS:
            already = session.execute(
                select(TradeLog).where(
                    TradeLog.exit_order_id == p.sell_order_id,
                    TradeLog.notes.contains(BACKFILL_MARKER),
                )
            ).scalars().first()
            if already:
                logger.info(f"skip（追加済み）: {p.ticker} sell_order={p.sell_order_id}")
                continue

            sell = session.get(Order, p.sell_order_id)
            lot = session.execute(
                select(TradeLog).where(TradeLog.entry_order_id == p.entry_order_id)
            ).scalars().first()
            if sell is None or lot is None or sell.ticker != p.ticker or lot.ticker != p.ticker:
                raise RuntimeError(f"照合できません: {p}（注文/ロットが見つからないか銘柄が不一致）")

            exit_price = p.fill_override or sell.filled_price
            if not exit_price:
                raise RuntimeError(f"約定価格が不明: {p}")
            pnl = (exit_price - lot.entry_price) * p.quantity
            pnl_pct = (exit_price / lot.entry_price - 1) * 100 if lot.entry_price else 0.0
            total += pnl
            print(
                f"{p.ticker:5} 売{p.quantity}株 @ ${exit_price:.2f} / 建値 ${lot.entry_price:.2f} "
                f"→ pnl ${pnl:+.2f} ({pnl_pct:+.2f}%)  [order {p.sell_order_id}, lot entry_order {p.entry_order_id}]"
            )
            if not apply:
                continue

            session.add(TradeLog(
                ticker=lot.ticker,
                entry_order_id=lot.entry_order_id,
                exit_order_id=sell.id,
                entry_date=lot.entry_date,
                exit_date=sell.created_at.date(),
                entry_price=lot.entry_price,
                exit_price=exit_price,
                highest_price=lot.highest_price,
                quantity=p.quantity,
                pnl=pnl,
                pnl_pct=pnl_pct,
                strategy_name=lot.strategy_name,
                stop_loss=lot.stop_loss,
                take_profit=lot.take_profit,
                take_profit_1=lot.take_profit_1,
                tp1_hit=True,
                max_hold_days=lot.max_hold_days,
                notes=f"{BACKFILL_MARKER}: 段階利確TP1 {p.quantity}株 @ ${exit_price:.2f}（実約定価格）",
                status="CLOSED",
            ))
            if p.fill_override and sell.status == "SUBMITTED":
                sell.status = "FILLED"
                sell.filled_price = p.fill_override
        if apply:
            session.commit()
    print(f"\n合計 pnl ${total:+.2f}（{'追加しました' if apply else 'dry-run: DBは未変更'}）")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="段階利確(TP1)の未計上分をtrade_logへバックフィル")
    parser.add_argument("--apply", action="store_true", help="実際にDBへ追加する（省略時はdry-run）")
    args = parser.parse_args()
    backfill(apply=args.apply)


if __name__ == "__main__":
    main()
