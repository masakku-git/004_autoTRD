"""市場環境の判定（S&P500/VIXからレジーム分類）と、レジームに適した戦略の選択"""
from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy import select

from src.data.fetcher import get_ohlcv
from src.models.base import get_session
from src.models.market import MarketCondition
from src.strategy.registry import get_strategies_for_regime
from src.utils.logger import logger

# S&P 500 ETF and VIX for market assessment
SP500_TICKER = "^GSPC"
VIX_TICKER = "^VIX"


def assess_market_condition() -> dict:
    """Assess current market condition and store in DB."""
    sp500_df = get_ohlcv(SP500_TICKER)
    vix_df = get_ohlcv(VIX_TICKER)

    condition = {
        "date": date.today(),
        "sp500_trend": "neutral",
        "vix_level": 0.0,
        "market_breadth": 0.0,
        "regime": "range",
    }

    if not sp500_df.empty:
        condition["sp500_trend"] = _assess_trend(sp500_df)

    if not vix_df.empty:
        condition["vix_level"] = float(vix_df["Close"].iloc[-1])

    condition["regime"] = _determine_regime(
        condition["sp500_trend"], condition["vix_level"]
    )

    _save_market_condition(condition)
    logger.info(
        f"Market: trend={condition['sp500_trend']}, "
        f"VIX={condition['vix_level']:.1f}, regime={condition['regime']}"
    )
    return condition


def _assess_trend(df: pd.DataFrame) -> str:
    """Assess trend using SMA200 and SMA50."""
    if len(df) < 200:
        return "neutral"

    close = df["Close"]
    sma50 = close.rolling(50).mean().iloc[-1]
    sma200 = close.rolling(200).mean().iloc[-1]
    current = close.iloc[-1]

    if current > sma200 and sma50 > sma200:
        return "bull"
    elif current < sma200 and sma50 < sma200:
        return "bear"
    return "neutral"


def _determine_regime(trend: str, vix: float) -> str:
    """Determine market regime from trend and volatility."""
    if vix > 30:
        return "volatile"
    if trend in ("bull", "bear"):
        return "trending"
    return "range"


def _save_market_condition(condition: dict) -> None:
    """Save market condition to DB (upsert by date)."""
    with get_session() as session:
        existing = session.execute(
            select(MarketCondition).where(
                MarketCondition.date == condition["date"]
            )
        ).scalar_one_or_none()

        if existing:
            existing.sp500_trend = condition["sp500_trend"]
            existing.vix_level = condition["vix_level"]
            existing.market_breadth = condition["market_breadth"]
            existing.regime = condition["regime"]
        else:
            record = MarketCondition(
                date=condition["date"],
                sp500_trend=condition["sp500_trend"],
                vix_level=condition["vix_level"],
                market_breadth=condition["market_breadth"],
                regime=condition["regime"],
            )
            session.add(record)
        session.commit()


def select_strategies(market_condition: dict):
    """Select strategies appropriate for current market regime."""
    regime = market_condition.get("regime", "range")
    strategies = get_strategies_for_regime(regime)
    logger.info(
        f"Selected {len(strategies)} strategies for regime '{regime}': "
        f"{[s.name for s in strategies]}"
    )
    return strategies
