"""注文執行 v2 — 段階利確（Staged TP1）の実現損益を trade_log.pnl に記録する修正版

v1 (executor.py) からの変更点:
- partial_close_trade_log: 行の一部だけを消化する段階決済で、売却分を
  独立した CLOSED 行として分割記録する。v1 では売却分の PnL が notes に
  文字列として残るだけで pnl 列に入らず、DB集計 (weekly report / 戦略別成績)
  から実現益が漏れていた（2026-04〜07 で合計 +$158.24 の計上漏れ）。
- 分割行は exit_order_id を持つため、reconcile_fills による実約定価格への
  補正も部分決済に対して機能するようになる。

place_order / create_trade_log / close_trade_log は v1 のものをそのまま再輸出する。
"""
from __future__ import annotations

from src.broker.executor import (  # noqa: F401  (v1から変更なしの関数を再輸出)
    close_trade_log,
    close_trade_log_by_id,
    create_trade_log,
    place_order,
)
from src.models.base import get_session
from src.models.trade import Order, TradeLog
from src.utils.helpers import utcnow
from src.utils.logger import logger


def partial_close_trade_log(
    ticker: str, exit_order: Order, exit_price: float, sold_qty: int
) -> None:
    """段階決済: FIFOで sold_qty 株分の OPEN 行を消化（複数OPEN対応）。

    - 古い行から順に消化し、qty が消化量以下の行は CLOSED に更新（行ごとPnL算出）。
    - 最後に残った行が部分消化なら、売却分を新規 CLOSED 行として分割し、
      元の行は quantity を減らして OPEN を継続。
    - 同銘柄の残OPEN行のTP1も全てクリアし、連続TP1発動を防ぐ。
    """
    from sqlalchemy import select

    with get_session() as session:
        trades = session.execute(
            select(TradeLog)
            .where(TradeLog.ticker == ticker, TradeLog.status == "OPEN")
            .order_by(TradeLog.entry_date)
        ).scalars().all()

        if not trades:
            return

        if len(trades) > 1:
            logger.warning(
                f"partial_close_trade_log: {ticker} has {len(trades)} OPEN rows — FIFO consume"
            )

        actual_exit_price = exit_order.filled_price or exit_price
        remaining = sold_qty
        total_pnl = 0.0

        for trade in trades:
            if remaining <= 0:
                # 既に売り切った後の残OPEN行：TP1だけクリア
                trade.take_profit_1 = None
                continue
            if trade.quantity <= remaining:
                # 行を全消化 → CLOSED
                qty_sold = trade.quantity
                pnl = (actual_exit_price - trade.entry_price) * qty_sold
                trade.exit_order_id = exit_order.id
                trade.exit_date = utcnow().date()
                trade.exit_price = actual_exit_price
                trade.pnl = pnl
                trade.pnl_pct = (
                    (actual_exit_price / trade.entry_price - 1) * 100
                    if trade.entry_price
                    else 0
                )
                trade.status = "CLOSED"
                note = (
                    f"段階決済(全消化): {qty_sold}株 @ ${actual_exit_price:.2f}, "
                    f"PnL=${pnl:.2f}"
                )
                trade.notes = f"{trade.notes or ''}\n{note}".strip()
                total_pnl += pnl
                remaining -= qty_sold
            else:
                # 行の一部を消化 → 売却分を独立した CLOSED 行に分割し、
                # 元の行は quantity を減らして OPEN 継続（pnl 集計漏れの修正点）
                qty_sold = remaining
                pnl = (actual_exit_price - trade.entry_price) * qty_sold
                closed_part = TradeLog(
                    ticker=trade.ticker,
                    entry_order_id=trade.entry_order_id,
                    exit_order_id=exit_order.id,
                    entry_date=trade.entry_date,
                    exit_date=utcnow().date(),
                    entry_price=trade.entry_price,
                    exit_price=actual_exit_price,
                    highest_price=trade.highest_price,
                    quantity=qty_sold,
                    pnl=pnl,
                    pnl_pct=(
                        (actual_exit_price / trade.entry_price - 1) * 100
                        if trade.entry_price
                        else 0
                    ),
                    strategy_name=trade.strategy_name,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit,
                    take_profit_1=None,
                    max_hold_days=trade.max_hold_days,
                    notes=(
                        f"段階決済(分割): trade_log id={trade.id} から "
                        f"{qty_sold}株分を分割クローズ"
                    ),
                    status="CLOSED",
                )
                session.add(closed_part)
                trade.quantity -= qty_sold
                trade.take_profit_1 = None  # TP1消費済み
                note = (
                    f"段階決済(部分): {qty_sold}株 @ ${actual_exit_price:.2f}, "
                    f"PnL=${pnl:.2f} → 分割CLOSED行に記録"
                )
                trade.notes = f"{trade.notes or ''}\n{note}".strip()
                total_pnl += pnl
                remaining = 0

        session.commit()
        logger.info(
            f"Partial close: {ticker} sold {sold_qty - remaining} shares, "
            f"PnL=${total_pnl:.2f}"
        )


def partial_close_trade_log_by_id(
    trade_id: int, exit_order: Order, exit_price: float, sold_qty: int
) -> None:
    """ロット単位の段階決済: 指定行だけを消化し、他ロットのTP1には触れない。

    partial_close_trade_log（ティッカー単位・FIFO・全OPEN行のTP1をクリア）とは異なり、
    ロットごとに独立してSL/TP1/TPを判定する運用向け。他ロットの状態は一切変更しない。
    決済処理そのものは close_trade_log_by_id と共通で、TP1消費のみここで指定する。
    """
    close_trade_log_by_id(
        trade_id, exit_order, exit_price, sold_qty=sold_qty, consume_tp1=True
    )
