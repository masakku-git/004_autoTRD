"""約定確認の遅延ジョブ — moomoo の実約定情報で Order/TradeLog を更新する。

main.py が 22:00 JST に発注した成行注文は、米国市場の寄付き（22:30〜23:30 JST）で
約定する。executor._poll_for_fill は60秒で打ち切るため、ほぼ全件で filled_price が
DB に反映されない（status='SUBMITTED' のまま）。

本スクリプトは寄付き完了後（systemd timer で 01:00 JST = 16:00 UTC）に走り、
moomoo から実約定情報を取得して Order と TradeLog を実際の dealt_avg_price で
更新する。

Usage:
    python scripts/reconcile_fills.py              # 当日分のみ
    python scripts/reconcile_fills.py --days 30    # 過去N日分（バックフィル用）
    python scripts/reconcile_fills.py --dry-run    # DBは更新せず差分のみ表示
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from config.settings import settings
from src.models.base import get_session
from src.models.trade import Order, TradeLog
from src.utils.logger import logger


def _trd_env():
    from moomoo import TrdEnv
    return TrdEnv.SIMULATE if settings.moomoo_trade_env == "SIMULATE" else TrdEnv.REAL


def _open_ctx():
    from moomoo import OpenSecTradeContext, SecurityFirm, TrdMarket
    return OpenSecTradeContext(
        host=settings.moomoo_host,
        port=settings.moomoo_port,
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.FUTUJP,
    )


def _fetch_broker_orders(days: int) -> dict:
    """moomoo から注文一覧を取得し、broker_order_id をキーにした dict を返す。"""
    ctx = _open_ctx()
    try:
        if days <= 1:
            ret, df = ctx.order_list_query(trd_env=_trd_env(), acc_id=settings.moomoo_acc_id)
        else:
            end = datetime.utcnow().date()
            start = end - timedelta(days=days)
            ret, df = ctx.history_order_list_query(
                start=start.isoformat(),
                end=end.isoformat(),
                trd_env=_trd_env(),
                acc_id=settings.moomoo_acc_id,
            )
        if ret != 0:
            raise RuntimeError(f"moomoo query failed: {df}")
        if df is None or df.empty:
            return {}
        return {str(row["order_id"]): row for _, row in df.iterrows()}
    finally:
        ctx.close()


def _update_trade_log(session, order: Order, actual_price: float) -> None:
    """Orderの side に応じて TradeLog の entry_price / exit_price と pnl を更新する。

    - BUY: entry_price を実約定価格に置き換え。CLOSED 済みなら pnl も再計算
    - SELL: trade_log.exit_order_id == order.id の trade を更新

    同一注文を複数の trade_log 行が参照するケースに対応するため全行を更新する
    （段階決済の分割CLOSED行が entry_order_id を共有する / 複数OPEN行の一括
    クローズが exit_order_id を共有する）。
    """
    if order.side == "BUY":
        trades = session.execute(
            select(TradeLog).where(TradeLog.entry_order_id == order.id)
        ).scalars().all()
        for trade in trades:
            old = trade.entry_price
            trade.entry_price = actual_price
            if trade.status == "CLOSED" and trade.exit_price is not None:
                trade.pnl = (trade.exit_price - actual_price) * trade.quantity
                if actual_price > 0:
                    trade.pnl_pct = (trade.exit_price / actual_price - 1) * 100
            logger.info(
                f"  → trade_log id={trade.id} {trade.ticker}: entry_price ${old:.2f} → ${actual_price:.2f}"
            )
    elif order.side == "SELL":
        trades = session.execute(
            select(TradeLog).where(TradeLog.exit_order_id == order.id)
        ).scalars().all()
        for trade in trades:
            old = trade.exit_price
            trade.exit_price = actual_price
            trade.pnl = (actual_price - trade.entry_price) * trade.quantity
            if trade.entry_price > 0:
                trade.pnl_pct = (actual_price / trade.entry_price - 1) * 100
            old_str = f"${old:.2f}" if old is not None else "None"
            logger.info(
                f"  → trade_log id={trade.id} {trade.ticker}: exit_price {old_str} → ${actual_price:.2f} "
                f"(pnl=${trade.pnl:.2f}, {trade.pnl_pct:.2f}%)"
            )


def reconcile(days: int, dry_run: bool) -> dict:
    """SUBMITTED な Order を moomoo の実約定情報で更新し、関連 TradeLog も補正する。"""
    broker_orders = _fetch_broker_orders(days)
    logger.info(f"moomoo から {len(broker_orders)} 件の注文情報を取得（過去{days}日分）")

    counts = {"filled": 0, "cancelled": 0, "partial": 0, "pending": 0, "missing": 0}

    with get_session() as session:
        rows = session.execute(
            select(Order).where(
                Order.status == "SUBMITTED",
                Order.broker_order_id.isnot(None),
            )
        ).scalars().all()

        logger.info(f"DB に SUBMITTED 状態の注文が {len(rows)} 件")

        for order in rows:
            broker = broker_orders.get(str(order.broker_order_id))
            if broker is None:
                logger.debug(f"order id={order.id}: moomoo側に該当なし（範囲外）")
                counts["missing"] += 1
                continue

            broker_status = str(broker["order_status"])
            dealt_price = float(broker.get("dealt_avg_price", 0) or 0)

            if broker_status == "FILLED_ALL" and dealt_price > 0:
                ref = order.price if order.price is not None else 0.0
                logger.info(
                    f"FILLED: order id={order.id} {order.side} {order.ticker} "
                    f"@ ${dealt_price:.2f} (ref=${ref:.2f}, "
                    f"diff={(dealt_price - ref):+.2f}/{((dealt_price/ref-1)*100 if ref else 0):+.2f}%)"
                )
                if not dry_run:
                    order.filled_price = dealt_price
                    order.filled_at = datetime.utcnow()
                    order.status = "FILLED"
                    _update_trade_log(session, order, dealt_price)
                counts["filled"] += 1

            elif broker_status in ("CANCELLED_ALL", "CANCELLED_PART", "FAILED"):
                logger.warning(
                    f"CANCELLED/FAILED: order id={order.id} {order.ticker} broker_status={broker_status}"
                )
                if not dry_run:
                    order.status = "CANCELLED"
                counts["cancelled"] += 1

            elif broker_status == "FILLED_PART":
                logger.warning(
                    f"PARTIAL: order id={order.id} {order.ticker} "
                    f"dealt_qty={broker.get('dealt_qty')}, avg=${dealt_price:.2f} — 要手動確認"
                )
                counts["partial"] += 1

            else:
                logger.info(f"PENDING: order id={order.id} {order.ticker} broker_status={broker_status}")
                counts["pending"] += 1

        if not dry_run:
            session.commit()

    logger.info(
        f"完了: filled={counts['filled']}, cancelled={counts['cancelled']}, "
        f"partial={counts['partial']}, pending={counts['pending']}, missing={counts['missing']} "
        f"(dry_run={dry_run})"
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="moomoo の実約定情報で Order/TradeLog を更新")
    parser.add_argument("--days", type=int, default=1, help="遡る日数（1=当日のみ、>=2でhistory_order_list_query）")
    parser.add_argument("--dry-run", action="store_true", help="DBを更新せず差分のみ表示")
    args = parser.parse_args()
    reconcile(days=args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
