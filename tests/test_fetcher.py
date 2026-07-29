"""Tests for src.data.fetcher NaN handling."""
import numpy as np
import pandas as pd

from src.data.fetcher import _drop_nan_rows


def _make_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.bdate_range("2026-07-01", periods=5),
            "Open": [10.0, 11.0, 12.0, 13.0, 14.0],
            "High": [10.5, 11.5, 12.5, 13.5, 14.5],
            "Low": [9.5, 10.5, 11.5, 12.5, 13.5],
            "Close": [10.2, 11.2, 12.2, 13.2, 14.2],
            "Adj Close": [10.2, 11.2, 12.2, 13.2, 14.2],
            "Volume": [1_000_000] * 5,
        }
    )


def test_drop_nan_rows_keeps_clean_df():
    df = _make_df()
    cleaned = _drop_nan_rows("TEST", df)
    assert len(cleaned) == 5


def test_drop_nan_rows_removes_nan_volume():
    df = _make_df()
    df.loc[2, "Volume"] = np.nan
    cleaned = _drop_nan_rows("TEST", df)
    assert len(cleaned) == 4
    # int(row["Volume"]) must be safe on every remaining row
    assert all(int(v) > 0 for v in cleaned["Volume"])


def test_drop_nan_rows_removes_nan_close():
    df = _make_df()
    df.loc[4, "Close"] = np.nan
    cleaned = _drop_nan_rows("TEST", df)
    assert len(cleaned) == 4
    assert not cleaned["Close"].isna().any()


def test_drop_nan_rows_all_nan_returns_empty():
    df = _make_df()
    df["Close"] = np.nan
    cleaned = _drop_nan_rows("TEST", df)
    assert cleaned.empty
