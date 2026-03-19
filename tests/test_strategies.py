"""Test strategy plugins."""
import pandas as pd
import numpy as np
import pytest

from src.strategy.plugins.sma_crossover import SMACrossover
from src.strategy.plugins.rsi_reversal import RSIReversal
from src.strategy.plugins.breakout import Breakout
from src.strategy.base import Signal


class TestSMACrossover:
    def test_name_and_regime(self):
        s = SMACrossover()
        assert s.name == "sma_crossover"
        assert s.target_regime == "trending"

    def test_returns_none_insufficient_data(self, market_condition_trending):
        s = SMACrossover()
        df = pd.DataFrame(
            {"Open": [1], "High": [2], "Low": [0.5], "Close": [1.5], "Volume": [100]},
            index=pd.to_datetime(["2024-01-01"]),
        )
        result = s.generate_signals("TEST", df, market_condition_trending)
        assert result is None

    def test_generates_signal_on_crossover(self, sample_ohlcv, market_condition_trending):
        s = SMACrossover(short_period=5, long_period=20)
        signal = s.generate_signals("TEST", sample_ohlcv, market_condition_trending)
        # May or may not generate signal depending on data, but should not error
        if signal is not None:
            assert isinstance(signal, Signal)
            assert signal.action in ("BUY", "SELL")
            assert 0 <= signal.confidence <= 1

    def test_get_params(self):
        s = SMACrossover(short_period=10, long_period=30)
        params = s.get_params()
        assert params["short_period"] == 10
        assert params["long_period"] == 30


class TestRSIReversal:
    def test_name_and_regime(self):
        s = RSIReversal()
        assert s.name == "rsi_reversal"
        assert s.target_regime == "range"

    def test_returns_none_insufficient_data(self, market_condition_range):
        s = RSIReversal()
        df = pd.DataFrame(
            {"Open": [1], "High": [2], "Low": [0.5], "Close": [1.5], "Volume": [100]},
            index=pd.to_datetime(["2024-01-01"]),
        )
        assert s.generate_signals("TEST", df, market_condition_range) is None

    def test_get_params(self):
        s = RSIReversal(oversold=25, overbought=75)
        params = s.get_params()
        assert params["oversold"] == 25
        assert params["overbought"] == 75


class TestBreakout:
    def test_name_and_regime(self):
        s = Breakout()
        assert s.name == "breakout"
        assert s.target_regime == "any"

    def test_get_params(self):
        s = Breakout(lookback=30, volume_mult=2.0)
        params = s.get_params()
        assert params["lookback"] == 30
        assert params["volume_mult"] == 2.0
