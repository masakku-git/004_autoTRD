"""Test fixtures."""
import sys
from pathlib import Path

import pytest

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np


@pytest.fixture
def sample_ohlcv():
    """Generate sample OHLCV data for testing."""
    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=200)
    close = 100 + np.cumsum(np.random.randn(200) * 0.5)
    df = pd.DataFrame(
        {
            "Open": close + np.random.randn(200) * 0.3,
            "High": close + abs(np.random.randn(200) * 0.5),
            "Low": close - abs(np.random.randn(200) * 0.5),
            "Close": close,
            "Adj Close": close,
            "Volume": np.random.randint(500_000, 5_000_000, 200),
        },
        index=dates,
    )
    return df


@pytest.fixture
def market_condition_trending():
    return {"regime": "trending", "sp500_trend": "bull", "vix_level": 18.0}


@pytest.fixture
def market_condition_range():
    return {"regime": "range", "sp500_trend": "neutral", "vix_level": 22.0}
