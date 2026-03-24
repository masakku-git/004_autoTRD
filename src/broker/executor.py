"""注文執行（moomoo APIへの発注・トレードログの記録・DRY_RUNモード対応）"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime

from config.settings import settings
from src.models.base import get_session
from src.models.trade import Order, TradeLog
from src.strategy.base import Signal
from src.utils.logger import logger


def place_order(signal: Signal, quantity: int) -> Order:
    """Place an order based on a trading signal.

    In DRY_RUN mode, logs the order but does not submit to broker.
    """
    order = Order(
        ticker=signal.ticker,
        side=signal.action,
        order_type="LIMIT",
        quantity=quantity,
        price=signal.stop_loss if signal.action == "SELL" else None,
        status="PENDING",
        strategy_name="unknown",
        created_at=datetime.utcnow(),
    )

    # Determine price: use current market for simplicity
    # In real implementation, would use limit price based on signal
    order.price = signal.take_profit if signal.action == "SELL" else signal.stop_loss

    with get_session() as session:
        session.add(order)
        session.flush()
        order_id = order.id

        if settings.dry_run:
            logger.info(
                f"DRY_RUN: {signal.action} {quantity} shares of {signal.ticker} "
                f"(strategy: {signal.reason[:50]})"
            )
            order.status = "DRY_RUN"
            session.commit()
            return order

        # Submit to moomoo
        try:
            broker_order_id = _submit_to_moomoo(signal, quantity)
            order.broker_order_id = broker_order_id
            order.status = "SUBMITTED"
            logger.info(
                f"Order submitted: {signal.action} {quantity}x {signal.ticker} "
                f"(broker_id={broker_order_id})"
            )
        except Exception as e:
            order.status = "FAILED"
            logger.error(f"Order failed for {signal.ticker}: {e}")

        session.commit()
        return order


MOOMOO_TIMEOUT = 30  # seconds


def _submit_to_moomoo(signal: Signal, quantity: int) -> str:
    """Submit order to moomoo via OpenD API. Returns broker order ID."""
    try:
        from moomoo import (
            OpenSecTradeContext,
            OrderType,
            TrdEnv,
            TrdMarket,
            TrdSide,
        )
    except ImportError:
        raise RuntimeError("moomoo-api package not installed")

    trd_env = TrdEnv.SIMULATE if settings.moomoo_trade_env == "SIMULATE" else TrdEnv.REAL
    side = TrdSide.BUY if signal.action == "BUY" else TrdSide.SELL

    def _place() -> str:
        ctx = OpenSecTradeContext(
            host=settings.moomoo_host,
            port=settings.moomoo_port,
            filter_trdmarket=TrdMarket.US,
        )
        try:
            if trd_env == TrdEnv.REAL and settings.moomoo_trade_password_md5:
                ret, msg = ctx.unlock_trade(settings.moomoo_trade_password_md5)
                if ret != 0:
                    raise RuntimeError(f"Failed to unlock trade: {msg}")

            moomoo_ticker = f"US.{signal.ticker}" if not signal.ticker.startswith("US.") else signal.ticker

            ret, data = ctx.place_order(
                price=0,
                qty=quantity,
                code=moomoo_ticker,
                trd_side=side,
                order_type=OrderType.MARKET,
                trd_env=trd_env,
            )

            if ret != 0:
                raise RuntimeError(f"Place order failed: {data}")

            return str(data["order_id"].iloc[0])
        finally:
            ctx.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_place)
        try:
            return future.result(timeout=MOOMOO_TIMEOUT)
        except FuturesTimeoutError:
            err = f"発注タイムアウト（{MOOMOO_TIMEOUT}秒）{signal.action} {quantity}x {signal.ticker}"
            logger.error(err)
            _notify_opend_error(err)
            raise RuntimeError(err)


def _notify_opend_error(message: str) -> None:
    """OpenD接続失敗時にSlack通知を送る（循環importを避けるため遅延import）"""
    try:
        from src.notify.notifier import send_notification
        send_notification("OpenD接続エラー", message, level="error")
    except Exception as notify_err:
        logger.error(f"Slack通知送信失敗: {notify_err}")


def create_trade_log(
    signal: Signal, order: Order, quantity: int
) -> None:
    """Create a trade log entry for a new position."""
    if signal.action != "BUY":
        return

    with get_session() as session:
        trade = TradeLog(
            ticker=signal.ticker,
            entry_order_id=order.id,
            entry_date=datetime.utcnow().date(),
            entry_price=order.price or 0,
            quantity=quantity,
            strategy_name=order.strategy_name,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            take_profit_1=signal.take_profit_1 if signal.take_profit_1 > 0 else None,
            max_hold_days=signal.max_hold_days,
            status="OPEN",
        )
        session.add(trade)
        session.commit()


def close_trade_log(
    ticker: str, exit_order: Order, exit_price: float
) -> None:
    """Close an open trade log entry."""
    from sqlalchemy import select

    with get_session() as session:
        trade = session.execute(
            select(TradeLog).where(
                TradeLog.ticker == ticker, TradeLog.status == "OPEN"
            )
        ).scalar_one_or_none()

        if trade:
            trade.exit_order_id = exit_order.id
            trade.exit_date = datetime.utcnow().date()
            trade.exit_price = exit_price
            trade.pnl = (exit_price - trade.entry_price) * trade.quantity
            trade.pnl_pct = (exit_price / trade.entry_price - 1) * 100 if trade.entry_price else 0
            trade.status = "CLOSED"
            session.commit()
            logger.info(
                f"Trade closed: {ticker} PnL=${trade.pnl:.2f} ({trade.pnl_pct:.1f}%)"
            )


def partial_close_trade_log(
    ticker: str, exit_order: Order, exit_price: float, sold_qty: int
) -> None:
    """段階決済: 一部を売却してTradeLogを更新（CLOSEDにはしない）。"""
    from sqlalchemy import select

    with get_session() as session:
        trade = session.execute(
            select(TradeLog).where(
                TradeLog.ticker == ticker, TradeLog.status == "OPEN"
            )
        ).scalar_one_or_none()

        if trade:
            partial_pnl = (exit_price - trade.entry_price) * sold_qty
            note = (
                f"段階決済: {sold_qty}株 @ ${exit_price:.2f}, "
                f"PnL=${partial_pnl:.2f}"
            )
            trade.quantity -= sold_qty
            trade.take_profit_1 = None  # TP1消費済み
            trade.notes = f"{trade.notes or ''}\n{note}".strip()
            session.commit()
            logger.info(f"Partial close: {ticker} {note}")
