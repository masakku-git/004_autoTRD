"""ポートフォリオの日次スナップショット（資産推移の追跡用）"""
from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Float, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class PortfolioSnapshot(Base):
    """日次の資産状態（総資産・現金・保有ポジション一覧）"""
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True)
    total_equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    positions_json: Mapped[list] = mapped_column(JSONB)
    num_positions: Mapped[int] = mapped_column(Integer)
