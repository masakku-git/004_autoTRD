"""Tests for src.data.screener NaN handling (regression for 2026-07-29 crash)."""
import numpy as np
import pandas as pd

from src.data.screener import (
    LOOKBACK_DAYS,
    calculate_relative_strength,
    screen_ticker,
)


def _make_df(rows: int = 60) -> pd.DataFrame:
    """Data that passes every screen_ticker filter (price/volume/ATR)."""
    close = pd.Series(np.linspace(50.0, 55.0, rows))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1.0,   # TR ~2 → ATR% ~4% (> MIN_ATR_PCT)
            "Low": close - 1.0,
            "Close": close,
            "Adj Close": close,
            "Volume": [1_000_000] * rows,
        }
    )


def test_screen_ticker_accepts_clean_data():
    result = screen_ticker("TEST", _make_df())
    assert result is not None
    assert not pd.isna(result["relative_strength"])


def test_relative_strength_nan_endpoint_returns_nan():
    df = _make_df()
    df.loc[len(df) - LOOKBACK_DAYS, "Close"] = np.nan
    assert pd.isna(calculate_relative_strength(df, LOOKBACK_DAYS))


def test_relative_strength_zero_base_returns_nan():
    df = _make_df()
    df.loc[len(df) - LOOKBACK_DAYS, "Close"] = 0.0
    assert pd.isna(calculate_relative_strength(df, LOOKBACK_DAYS))


def test_screen_ticker_rejects_nan_relative_strength():
    """Close NaN at -LOOKBACK_DAYS slips past the ATR guard (ATR only looks at
    the last 14 rows) but must not produce a NaN score → JSONB insert crash."""
    df = _make_df()
    df.loc[len(df) - LOOKBACK_DAYS, "Close"] = np.nan
    assert screen_ticker("TEST", df) is None


def test_screen_ticker_rejects_nan_last_close():
    df = _make_df()
    df.loc[len(df) - 1, "Close"] = np.nan
    assert screen_ticker("TEST", df) is None
