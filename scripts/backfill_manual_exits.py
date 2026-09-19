"""手動決済・自動クローズで実績が欠けた5行を、証券会社の約定履歴どおりに補正する（一回限り）。

2026-04-22 に一般口座の銘柄を moomoo アプリから手動で売却したが、システムは気づかず、
5/16 に「moomooに保有なし」として自動クローズした。その結果、trade_log の次の行は
pnl が空欄、または決済日・決済価格が実態とずれている:
    id 4  AMZN 3株  4/22 $253.61 で売却   （決済価格が別の売却の $249.18 になっていた）
    id 8  MS   3株  4/22 $191.74 で売却
    id 11 AMD  2株  4/22 $299.14 で売却   （決済価格が別ロットの $325.90 になっていた）
    id 12 LOW  1株  4/22 $245.93 で売却
    id 16 AMD  1株  4/29 $325.90 で売却   （システムの注文 orders.id=24。pnlだけ空欄）
手動売却の4件は orders に SELL 注文が無いので追加する（手数料は次回の
reconcile_fills.py が order_fee_query で自動取得する）。

数値は moomoo の history_order_list_query / history_deal_list_query（2026-09-19 取得）で
突合済み。適用前に各行が想定どおりの状態か検証し、違えば何も変更せず中止する。

使い方:
    python scripts/backfill_manual_exits.py            # dry-run（DBは変更しない）
    python scripts/backfill_manual_exits.py --apply    # 実際に補正する

冪等: 補正済みの行は notes のマーカーで判定して飛ばす。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from src.models.base import get_session
from src.models.trade import Order, TradeLog
from src.utils.logger import logger

MARKER = "実績補正(手動決済/自動クローズの修正)"
_ET = ZoneInfo("America/New_York")


def _et_to_utc(text: str) -> datetime:
    """moomoo 履歴の時刻（米国東部時間）を naive UTC に変換（DBはnaive UTCで保持）。"""
    et = datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=_ET)
    return et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


@dataclass(frozen=True)
class Fix:
    trade_id: int
    ticker: str
    quantity: int
    entry_price: float                 # 適用前の検証用（DBの建値と一致すること）
    exit_price: float                  # 実約定価格
    # 手動売却: ordersに追加する注文。None なら既存の exit_order_id をそのまま使う
    manual_broker_order_id: str | None = None
    order_created_et: str | None = None
    filled_et: str | None = None
    existing_exit_order_id: int | None = None


FIXES = [
    Fix(4, "AMZN", 3, 235.38, 253.61, "FJ1C68693118A1F000", "2026-04-22 13:16:49.064", "2026-04-22 13:23:06.460"),
    Fix(8, "MS", 3, 190.62, 191.74, "FJ1C68695A41E1F000", "2026-04-22 13:17:31.211", "2026-04-22 13:22:15.366"),
    Fix(11, "AMD", 2, 278.26, 299.14, "FJ1C68691F58A1F000", "2026-04-22 13:16:30.886", "2026-04-22 13:17:50.715"),
    Fix(12, "LOW", 1, 252.00, 245.93, "FJ1C686948995F8000", "2026-04-22 13:17:13.128", "2026-04-22 13:17:13.812"),
    Fix(16, "AMD", 1, 347.02, 325.90, existing_exit_order_id=24),
]


def apply_fixes(apply: bool) -> float:
    total = 0.0
    with get_session() as session:
        # ---- 1. 事前検証（1件でも想定外なら何も変更せず中止） ----
        plan = []
        for f in FIXES:
            row = session.get(TradeLog, f.trade_id)
            if row is None:
                raise RuntimeError(f"trade_log id={f.trade_id} が見つかりません")
            if MARKER in (row.notes or ""):
                logger.info(f"skip（補正済み）: id={f.trade_id} {f.ticker}")
                continue
            if (row.ticker, row.quantity, row.status) != (f.ticker, f.quantity, "CLOSED") \
                    or abs(row.entry_price - f.entry_price) > 0.005 or row.pnl is not None:
                raise RuntimeError(
                    f"id={f.trade_id} が想定と違います: ticker={row.ticker} qty={row.quantity} "
                    f"status={row.status} entry={row.entry_price} pnl={row.pnl}"
                )
            if f.manual_broker_order_id:
                dup = session.execute(
                    select(Order).where(Order.broker_order_id == f.manual_broker_order_id)
                ).scalars().first()
                if dup is not None:
                    raise RuntimeError(f"broker注文 {f.manual_broker_order_id} は既に orders にあります(id={dup.id})")
            plan.append((f, row))

        # ---- 2. 補正内容の表示と適用 ----
        for f, row in plan:
            pnl = (f.exit_price - row.entry_price) * f.quantity
            pnl_pct = (f.exit_price / row.entry_price - 1) * 100
            total += pnl
            if f.manual_broker_order_id:
                created = _et_to_utc(f.order_created_et)
                filled = _et_to_utc(f.filled_et)
                exit_date = created.date()
                how = f"手動売却の注文を追加 ({f.manual_broker_order_id})"
            else:
                order = session.get(Order, f.existing_exit_order_id)
                if order is None or order.ticker != f.ticker or order.filled_price != f.exit_price:
                    raise RuntimeError(f"既存注文 id={f.existing_exit_order_id} が想定と違います")
                exit_date = order.created_at.date()
                how = f"既存注文 orders.id={order.id} を使用"
            print(
                f"id={f.trade_id:<3} {f.ticker:5} {f.quantity}株 建値${row.entry_price:.2f} → "
                f"@${f.exit_price:.2f} ({exit_date})  pnl ${pnl:+.2f} ({pnl_pct:+.2f}%)  [{how}]"
            )
            if not apply:
                continue

            if f.manual_broker_order_id:
                order = Order(
                    broker_order_id=f.manual_broker_order_id, ticker=f.ticker, side="SELL",
                    order_type="MANUAL", quantity=f.quantity, price=None, status="FILLED",
                    filled_price=f.exit_price, filled_at=filled, strategy_name="manual",
                    created_at=created,
                )
                session.add(order)
                session.flush()  # order.id を確定
            row.exit_order_id = order.id
            row.exit_price = f.exit_price
            row.exit_date = exit_date
            row.pnl = pnl
            row.pnl_pct = pnl_pct
            row.notes = f"{row.notes or ''}\n{MARKET_NOTE(f, pnl)}".strip()
        if apply:
            session.commit()
    print(f"\n合計 pnl ${total:+.2f}（{'補正しました' if apply else 'dry-run: DBは未変更'}）")
    return total


def MARKET_NOTE(f: Fix, pnl: float) -> str:
    kind = "手動売却" if f.manual_broker_order_id else "システム注文"
    return f"[{MARKER}] {kind} {f.quantity}株 @ ${f.exit_price:.2f} pnl=${pnl:.2f}（約定履歴で確認）"


def main() -> None:
    parser = argparse.ArgumentParser(description="手動決済/自動クローズで欠けた実績を約定履歴どおりに補正")
    parser.add_argument("--apply", action="store_true", help="実際にDBを補正する（省略時はdry-run）")
    args = parser.parse_args()
    apply_fixes(apply=args.apply)


if __name__ == "__main__":
    main()
