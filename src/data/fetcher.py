"""株価データ取得（yfinanceからOHLCVを取得しDBにキャッシュ、差分更新対応）"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from sqlalchemy import func, select

from src.models.base import get_session
from src.models.price import PriceCache
from src.utils.logger import logger

# Default history length for first fetch
DEFAULT_HISTORY_YEARS = 2
# Rate limit between yfinance calls
FETCH_DELAY_SEC = 0.5


def get_last_cached_date(ticker: str) -> date | None:
    """Get the most recent date cached in DB for a ticker."""
    with get_session() as session:
        result = session.execute(
            select(func.max(PriceCache.date)).where(PriceCache.ticker == ticker)
        ).scalar()
        return result


def fetch_from_yfinance(
    ticker: str, start: date, end: date | None = None
) -> pd.DataFrame:
    """Fetch OHLCV data from yfinance."""
    end = end or date.today()
    logger.info(f"Fetching {ticker} from yfinance: {start} to {end}")
    try:
        df = yf.download(
            ticker, start=str(start), end=str(end), progress=False, auto_adjust=False
        )
        if df.empty:
            logger.warning(f"No data returned for {ticker}")
            return pd.DataFrame()
        # Flatten multi-level columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        return df
    except Exception as e:
        logger.error(f"Failed to fetch {ticker}: {e}")
        return pd.DataFrame()


def save_to_cache(ticker: str, df: pd.DataFrame) -> int:
    """Save OHLCV DataFrame to DB cache. Returns number of rows inserted."""
    if df.empty:
        return 0
    rows_inserted = 0
    with get_session() as session:
        for _, row in df.iterrows():
            row_date = pd.Timestamp(row["Date"]).date()
            # Skip if already cached
            existing = session.execute(
                select(PriceCache.id).where(
                    PriceCache.ticker == ticker, PriceCache.date == row_date
                )
            ).scalar()
            if existing:
                continue
            record = PriceCache(
                ticker=ticker,
                date=row_date,
                open=float(row["Open"]),
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                adj_close=float(row.get("Adj Close", row["Close"])),
                volume=int(row["Volume"]),
            )
            session.add(record)
            rows_inserted += 1
        session.commit()
    logger.info(f"Cached {rows_inserted} new rows for {ticker}")
    return rows_inserted


def update_price_cache(ticker: str) -> int:
    """Fetch and cache price data. Delta fetch if data already exists."""
    last_date = get_last_cached_date(ticker)
    if last_date:
        start = last_date + timedelta(days=1)
    else:
        start = date.today() - timedelta(days=365 * DEFAULT_HISTORY_YEARS)

    if start >= date.today():
        logger.debug(f"{ticker} cache is up to date")
        return 0

    df = fetch_from_yfinance(ticker, start)
    return save_to_cache(ticker, df)


def update_price_cache_batch(tickers: list[str]) -> dict[str, int]:
    """Update cache for multiple tickers with rate limiting."""
    results = {}
    for i, ticker in enumerate(tickers):
        results[ticker] = update_price_cache(ticker)
        if i < len(tickers) - 1:
            time.sleep(FETCH_DELAY_SEC)
    return results


def get_ohlcv(
    ticker: str,
    start: date | None = None,
    end: date | None = None,
    ensure_updated: bool = True,
) -> pd.DataFrame:
    """Get OHLCV data from DB cache, optionally updating first."""
    if ensure_updated:
        update_price_cache(ticker)

    with get_session() as session:
        query = select(PriceCache).where(PriceCache.ticker == ticker)
        if start:
            query = query.where(PriceCache.date >= start)
        if end:
            query = query.where(PriceCache.date <= end)
        query = query.order_by(PriceCache.date)

        rows = session.execute(query).scalars().all()
        if not rows:
            return pd.DataFrame()

        data = [
            {
                "Date": r.date,
                "Open": r.open,
                "High": r.high,
                "Low": r.low,
                "Close": r.close,
                "Adj Close": r.adj_close,
                "Volume": r.volume,
            }
            for r in rows
        ]
        df = pd.DataFrame(data)
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        return df
