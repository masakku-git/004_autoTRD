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


def place_order(signal: Signal, quantity: int, strategy_name: str = "unknown") -> Order:
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
        strategy_name=strategy_name,
        created_at=datetime.utcnow(),
    )

    # Determine price: use current market for simplicity
    # In real implementation, would use limit price based on signal
    order.price = signal.take_profit if signal.action == "SELL" else signal.price

    with get_session() as session:
        session.add(order)
        session.flush()
        order_id = order.id

        if settings.dry_run:
            logger.info(
                f"DRY_RUN: {signal.action} {quantity} shares of {signal.ticker} "
                f"(strategy: {signal.reason[:50]})"
            )
            order.filled_price = signal.price or order.price
            order.filled_at = datetime.utcnow()
            order.status = "DRY_RUN"
            session.commit()
            return order

        # Submit to moomoo
        # 約定確認は scripts/reconcile_fills.py（systemd timer で米国市場クローズ後に起動）が
        # 別途行うため、ここでは SUBMITTED で確定する。daily run の発注タイミング(22:00 JST)では
        # 米国市場が開く前で寄付き約定を即時確認できないため、ポーリングは不要。
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
            SecurityFirm,
            SubAccType,
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
            security_firm=SecurityFirm.FUTUJP,
        )
        try:
            if trd_env == TrdEnv.REAL and settings.moomoo_trade_password_md5:
                ret, msg = ctx.unlock_trade(password_md5=settings.moomoo_trade_password_md5)
                if ret != 0:
                    raise RuntimeError(f"Failed to unlock trade: {msg}")

            moomoo_ticker = f"US.{signal.ticker}" if not signal.ticker.startswith("US.") else signal.ticker

            try:
                sub_acc_type = getattr(SubAccType, settings.moomoo_jp_acc_type)
            except AttributeError as exc:
                raise RuntimeError(
                    f"Invalid moomoo_jp_acc_type='{settings.moomoo_jp_acc_type}'. "
                    f"有効値は SubAccType Enum（JP_GENERAL / JP_TOKUTEI / JP_NISA_GENERAL 等）"
                ) from exc

            ret, data = ctx.place_order(
                price=0,
                qty=quantity,
                code=moomoo_ticker,
                trd_side=side,
                order_type=OrderType.MARKET,
                trd_env=trd_env,
                acc_id=settings.moomoo_acc_id,
                jp_acc_type=sub_acc_type,
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
    """Create a trade log entry for a new position.

    発注失敗時（FAILED/PENDING）は実ポジションが存在しないためtrade_logを作成しない。
    SUBMITTED / FILLED / DRY_RUN のみ記録対象とする。
    """
    if signal.action != "BUY":
        return
    if order.status in ("FAILED", "PENDING"):
        logger.info(
            f"Skip trade_log creation: {signal.ticker} order status={order.status} "
            f"(no actual position)"
        )
        return

    with get_session() as session:
        trade = TradeLog(
            ticker=signal.ticker,
            entry_order_id=order.id,
            entry_date=datetime.utcnow().date(),
            entry_price=order.filled_price or order.price or 0,
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
    """Close all OPEN trade log entries for this ticker.

    同銘柄に複数のOPEN行が存在しても全件をCLOSEDに更新する（クラッシュ回避）。
    各行は自身のentry_priceで個別にPnLを計算する。
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
                f"close_trade_log: {ticker} has {len(trades)} OPEN rows — closing all"
            )

        actual_exit_price = exit_order.filled_price or exit_price
        total_pnl = 0.0
        total_qty = 0
        for trade in trades:
            trade.exit_order_id = exit_order.id
            trade.exit_date = datetime.utcnow().date()
            trade.exit_price = actual_exit_price
            trade.pnl = (actual_exit_price - trade.entry_price) * trade.quantity
            trade.pnl_pct = (
                (actual_exit_price / trade.entry_price - 1) * 100
                if trade.entry_price
                else 0
            )
            trade.status = "CLOSED"
            total_pnl += trade.pnl
            total_qty += trade.quantity

        session.commit()

        if len(trades) == 1:
            t = trades[0]
            logger.info(
                f"Trade closed: {ticker} PnL=${t.pnl:.2f} ({t.pnl_pct:.1f}%)"
            )
        else:
            logger.info(
                f"Trade closed: {ticker} PnL=${total_pnl:.2f} total "
                f"({len(trades)} rows, {total_qty} shares)"
            )


def partial_close_trade_log(
    ticker: str, exit_order: Order, exit_price: float, sold_qty: int
) -> None:
    """段階決済: FIFOで sold_qty 株分の OPEN 行を消化（複数OPEN対応）。

    - 古い行から順に消化し、qty が消化量以下の行は CLOSED に更新（行ごとPnL算出）。
    - 最後に残った行が部分消化なら quantity を減らして OPEN を継続。
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
                trade.exit_date = datetime.utcnow().date()
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
                # 行の一部を消化 → qty を減らして OPEN 継続
                qty_sold = remaining
                pnl = (actual_exit_price - trade.entry_price) * qty_sold
                trade.quantity -= qty_sold
                trade.take_profit_1 = None  # TP1消費済み
                note = (
                    f"段階決済(部分): {qty_sold}株 @ ${actual_exit_price:.2f}, "
                    f"PnL=${pnl:.2f}"
                )
                trade.notes = f"{trade.notes or ''}\n{note}".strip()
                total_pnl += pnl
                remaining = 0

        session.commit()
        logger.info(
            f"Partial close: {ticker} sold {sold_qty - remaining} shares, "
            f"PnL=${total_pnl:.2f}"
        )
