"""注文(Order)とトレード履歴(TradeLog)のDBモデル"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class Order(Base):
    """発注レコード（DRY_RUN・実注文の両方を記録）"""
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # moomoo側の注文ID
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    side: Mapped[str] = mapped_column(String(4))       # BUY / SELL
    order_type: Mapped[str] = mapped_column(String(10))  # MARKET / LIMIT
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(15), default="PENDING")  # PENDING/SUBMITTED/FILLED/FAILED/DRY_RUN
    filled_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 約定価格
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # 約定日時
    strategy_name: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TradeLog(Base):
    """トレード履歴（エントリー〜エグジットを1レコードで管理、PnL計算用）"""
    __tablename__ = "trade_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    entry_order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))      # 買い注文ID
    exit_order_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("orders.id"), nullable=True                                  # 売り注文ID（決済時に記録）
    )
    entry_date: Mapped[date] = mapped_column(Date)
    exit_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)        # 損益（ドル）
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)    # 損益率（%）
    strategy_name: Mapped[str] = mapped_column(String(50))
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit_1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 段階利確の第1ターゲット
    max_hold_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=20)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="OPEN")  # OPEN=保有中 / CLOSED=決済済み
