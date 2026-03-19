from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import Date, Float, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class MarketCondition(Base):
    __tablename__ = "market_conditions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True)
    sp500_trend: Mapped[str] = mapped_column(String(10))  # bull/bear/neutral
    vix_level: Mapped[float] = mapped_column(Float)
    market_breadth: Mapped[float] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(20))  # trending/range/volatile
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ScreeningResult(Base):
    __tablename__ = "screening_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date] = mapped_column(Date, index=True)
    ticker: Mapped[str] = mapped_column(String(10))
    score: Mapped[float] = mapped_column(Float)
    criteria_json: Mapped[dict] = mapped_column(JSONB)
    selected: Mapped[bool] = mapped_column(default=False)
