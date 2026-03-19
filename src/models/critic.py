"""Database model for Devil's Advocate (critic) evaluation records."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class CriticEvaluation(Base):
    """Record of each signal evaluation by the Devil's Advocate agent.

    Stores the original signal, each objection raised, the adjusted confidence,
    and the final verdict. Used for PDCA review of the critic's accuracy.
    """

    __tablename__ = "critic_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(10), index=True)
    action: Mapped[str] = mapped_column(String(4))  # BUY/SELL
    strategy_name: Mapped[str] = mapped_column(String(50))
    original_confidence: Mapped[float] = mapped_column(Float)
    adjusted_confidence: Mapped[float] = mapped_column(Float)
    objections: Mapped[list] = mapped_column(JSONB)  # list of {check, penalty, reason}
    num_objections: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(10))  # APPROVED/REJECTED
    verdict_reason: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
