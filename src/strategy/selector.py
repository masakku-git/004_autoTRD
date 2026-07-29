"""市場環境の判定（S&P500/VIXからレジーム分類）と、レジームに適した戦略の選択"""
from __future__ import annotations

from datetime import date, timedelta

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
    """Assess current market condition and store in DB.

    ^GSPC/^VIXが取得できない場合は data_degraded=True を立てて返す。
    デフォルト値(vix_level=0.0/neutral)のままだとレジームが"range"に化けて
    リスク縮小判定(risk/manager)が素通りするため、劣化時は前回値（3日以内）
    または防御的レジーム(volatile)に退避し、劣化値ではDBを更新しない。
    呼び出し側は data_degraded を見て新規エントリーを停止すること。
    """
    sp500_df = get_ohlcv(SP500_TICKER)
    vix_df = get_ohlcv(VIX_TICKER)

    condition = {
        "date": date.today(),
        "sp500_trend": "neutral",
        "vix_level": 0.0,
        "market_breadth": 0.0,
        "regime": "range",
        "data_degraded": False,
    }

    sp500_ok = not sp500_df.empty and not pd.isna(sp500_df["Close"].iloc[-1])
    vix_ok = not vix_df.empty and not pd.isna(vix_df["Close"].iloc[-1])

    if not (sp500_ok and vix_ok):
        condition["data_degraded"] = True
        missing = [
            t for t, ok in ((SP500_TICKER, sp500_ok), (VIX_TICKER, vix_ok)) if not ok
        ]
        logger.error(f"市場データ取得失敗: {missing} — 前回値/防御的レジームで継続")
        prev = _load_recent_condition()
        if prev:
            condition["sp500_trend"] = prev["sp500_trend"]
            condition["vix_level"] = prev["vix_level"]
            condition["regime"] = prev["regime"]
            logger.warning(
                f"前回({prev['date']})の市場環境を流用: regime={prev['regime']}, "
                f"VIX={prev['vix_level']:.1f}"
            )
        else:
            condition["regime"] = "volatile"
            logger.warning("流用可能な前回値なし — 防御的レジーム(volatile)で継続")
        return condition

    condition["sp500_trend"] = _assess_trend(sp500_df)
    # RSI逆張り戦略のベア相場判定用（S&P500終値 vs 200日SMA）
    if len(sp500_df) >= 200:
        close = sp500_df["Close"]
        condition["sp500_close"] = float(close.iloc[-1])
        condition["sp500_sma200"] = float(close.rolling(200).mean().iloc[-1])

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


def _load_recent_condition(max_age_days: int = 3) -> dict | None:
    """直近max_age_days日以内のMarketConditionレコードをdictで返す（無ければNone）。"""
    cutoff = date.today() - timedelta(days=max_age_days)
    with get_session() as session:
        row = session.execute(
            select(MarketCondition)
            .where(MarketCondition.date >= cutoff)
            .order_by(MarketCondition.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "date": row.date,
            "sp500_trend": row.sp500_trend,
            "vix_level": row.vix_level,
            "regime": row.regime,
        }


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
