"""Market screener for candidate stock selection."""
from __future__ import annotations

from datetime import date

import pandas as pd

from src.data.fetcher import get_ohlcv, update_price_cache_batch
from src.models.base import get_session
from src.models.market import ScreeningResult
from src.utils.logger import logger

# Default universe: S&P 500 large-cap tickers (curated subset for MVP)
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B",
    "UNH", "JNJ", "V", "XOM", "JPM", "PG", "MA", "HD", "CVX", "MRK",
    "ABBV", "LLY", "PEP", "KO", "COST", "AVGO", "WMT", "MCD", "CSCO",
    "ACN", "TMO", "ABT", "DHR", "CRM", "NKE", "ORCL", "TXN", "AMD",
    "PM", "UPS", "NEE", "UNP", "LOW", "MS", "GS", "BLK", "ISRG",
    "INTC", "QCOM", "AMAT", "ADP", "SBUX",
]

# Screening thresholds
MIN_AVG_VOLUME = 500_000
MIN_PRICE = 5.0
MAX_PRICE = 500.0
MIN_ATR_PCT = 1.0  # Minimum ATR as % of price (need volatility for swing)
LOOKBACK_DAYS = 20


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_relative_strength(df: pd.DataFrame, period: int = 20) -> float:
    """Calculate relative strength as percentage return over period."""
    if len(df) < period:
        return 0.0
    return (df["Close"].iloc[-1] / df["Close"].iloc[-period] - 1) * 100


def screen_ticker(ticker: str, df: pd.DataFrame) -> dict | None:
    """Screen a single ticker. Returns criteria dict or None if rejected."""
    if len(df) < LOOKBACK_DAYS:
        return None

    recent = df.tail(LOOKBACK_DAYS)
    last_close = recent["Close"].iloc[-1]
    avg_volume = recent["Volume"].mean()

    # Price filter
    if last_close < MIN_PRICE or last_close > MAX_PRICE:
        return None

    # Volume filter
    if avg_volume < MIN_AVG_VOLUME:
        return None

    # ATR filter (need enough volatility for swing trading)
    atr = calculate_atr(df)
    if atr.empty or pd.isna(atr.iloc[-1]):
        return None
    atr_pct = (atr.iloc[-1] / last_close) * 100
    if atr_pct < MIN_ATR_PCT:
        return None

    # Relative strength
    rs = calculate_relative_strength(df, LOOKBACK_DAYS)

    return {
        "ticker": ticker,
        "last_close": float(round(last_close, 2)),
        "avg_volume": int(avg_volume),
        "atr": float(round(atr.iloc[-1], 2)),
        "atr_pct": float(round(atr_pct, 2)),
        "relative_strength": float(round(rs, 2)),
    }


def run_screening(
    universe: list[str] | None = None, top_n: int = 15
) -> list[dict]:
    """Run full screening process. Returns top N candidates sorted by score."""
    universe = universe or DEFAULT_UNIVERSE
    logger.info(f"Screening {len(universe)} tickers")

    # Update price cache for universe
    update_price_cache_batch(universe)

    candidates = []
    for ticker in universe:
        df = get_ohlcv(ticker, ensure_updated=False)
        if df.empty:
            continue
        result = screen_ticker(ticker, df)
        if result:
            # Composite score: weight RS and ATR
            result["score"] = float(result["relative_strength"] * 0.6 + result["atr_pct"] * 0.4)
            candidates.append(result)

    # Sort by composite score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = candidates[:top_n]

    # Save to DB
    _save_screening_results(selected, candidates)

    logger.info(f"Screening complete: {len(selected)}/{len(candidates)} candidates selected")
    return selected


def _save_screening_results(selected: list[dict], all_candidates: list[dict]) -> None:
    """Save screening results to database."""
    today = date.today()
    selected_tickers = {c["ticker"] for c in selected}

    with get_session() as session:
        for candidate in all_candidates:
            result = ScreeningResult(
                run_date=today,
                ticker=candidate["ticker"],
                score=candidate["score"],
                criteria_json=candidate,
                selected=candidate["ticker"] in selected_tickers,
            )
            session.add(result)
        session.commit()
