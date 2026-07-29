"""Tests for assess_market_condition degraded-data fallback."""
import numpy as np
import pandas as pd
import pytest

import src.strategy.selector as selector


def _ohlcv(close_values) -> pd.DataFrame:
    close = pd.Series(close_values, dtype=float)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Adj Close": close,
            "Volume": [1_000_000] * len(close),
        }
    )


@pytest.fixture
def no_db(monkeypatch):
    """Block DB access from assess_market_condition."""
    saved = []
    monkeypatch.setattr(selector, "_save_market_condition", lambda c: saved.append(c))
    monkeypatch.setattr(selector, "_load_recent_condition", lambda max_age_days=3: None)
    return saved


def test_normal_path_not_degraded(monkeypatch, no_db):
    data = {
        selector.SP500_TICKER: _ohlcv(np.linspace(4000, 5000, 250)),
        selector.VIX_TICKER: _ohlcv([15.0] * 30),
    }
    monkeypatch.setattr(selector, "get_ohlcv", lambda t, **kw: data[t])

    condition = selector.assess_market_condition()
    assert condition["data_degraded"] is False
    assert condition["vix_level"] == 15.0
    assert len(no_db) == 1  # saved to DB


def test_empty_data_is_degraded_volatile(monkeypatch, no_db):
    monkeypatch.setattr(selector, "get_ohlcv", lambda t, **kw: pd.DataFrame())

    condition = selector.assess_market_condition()
    assert condition["data_degraded"] is True
    assert condition["regime"] == "volatile"
    assert no_db == []  # degraded values must not be persisted


def test_nan_close_is_degraded(monkeypatch, no_db):
    vix = _ohlcv([15.0] * 30)
    vix.loc[len(vix) - 1, "Close"] = np.nan
    data = {
        selector.SP500_TICKER: _ohlcv(np.linspace(4000, 5000, 250)),
        selector.VIX_TICKER: vix,
    }
    monkeypatch.setattr(selector, "get_ohlcv", lambda t, **kw: data[t])

    condition = selector.assess_market_condition()
    assert condition["data_degraded"] is True


def test_degraded_reuses_previous_condition(monkeypatch, no_db):
    monkeypatch.setattr(selector, "get_ohlcv", lambda t, **kw: pd.DataFrame())
    monkeypatch.setattr(
        selector,
        "_load_recent_condition",
        lambda max_age_days=3: {
            "date": "2026-07-28",
            "sp500_trend": "bull",
            "vix_level": 32.0,
            "regime": "volatile",
        },
    )

    condition = selector.assess_market_condition()
    assert condition["data_degraded"] is True
    assert condition["sp500_trend"] == "bull"
    assert condition["vix_level"] == 32.0  # risk manager still sees high VIX
    assert no_db == []
