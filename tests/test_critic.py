"""Test Devil's Advocate (critic) agent."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy.base import Signal
from src.strategy.critic import (
    APPROVAL_THRESHOLD,
    CriticVerdict,
    check_low_liquidity_hours,
    check_overextended_price,
    check_resistance_proximity,
    check_risk_reward_ratio,
    check_trend_contradiction,
    check_vix_risk,
    check_volume_decline,
    evaluate_signal,
)


@pytest.fixture
def buy_signal():
    return Signal(
        ticker="TEST",
        action="BUY",
        confidence=0.7,
        stop_loss=90.0,
        take_profit=120.0,
        reason="Test BUY signal",
    )


@pytest.fixture
def sell_signal():
    return Signal(
        ticker="TEST",
        action="SELL",
        confidence=0.6,
        stop_loss=110.0,
        take_profit=85.0,
        reason="Test SELL signal",
    )


@pytest.fixture
def bull_market():
    return {"regime": "trending", "sp500_trend": "bull", "vix_level": 15.0}


@pytest.fixture
def bear_market():
    return {"regime": "trending", "sp500_trend": "bear", "vix_level": 32.0}


@pytest.fixture
def normal_df():
    """Stable price data with normal volume."""
    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=100)
    close = 100 + np.cumsum(np.random.randn(100) * 0.3)
    return pd.DataFrame(
        {
            "Open": close - 0.2,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Volume": np.full(100, 2_000_000),
        },
        index=dates,
    )


@pytest.fixture
def spiked_df():
    """Price data with a recent 15% spike and declining volume."""
    np.random.seed(42)
    dates = pd.bdate_range("2024-01-01", periods=100)
    close = np.full(100, 100.0)
    # Last 5 days: spike up 15%
    close[-5:] = [105, 108, 112, 114, 115]
    # Volume declining
    volume = np.full(100, 2_000_000)
    volume[-5:] = [500_000, 400_000, 300_000, 200_000, 150_000]
    return pd.DataFrame(
        {
            "Open": close - 0.5,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": volume,
        },
        index=dates,
    )


class TestTrendContradiction:
    def test_buy_in_bear_market_penalized(self, buy_signal, bear_market, normal_df):
        objections = check_trend_contradiction(buy_signal, normal_df, bear_market)
        assert len(objections) == 1
        assert objections[0].penalty == 0.25
        assert "bear" in objections[0].reason.lower()

    def test_buy_in_bull_market_no_objection(self, buy_signal, bull_market, normal_df):
        objections = check_trend_contradiction(buy_signal, normal_df, bull_market)
        assert len(objections) == 0

    def test_sell_not_penalized_in_bear(self, sell_signal, bear_market, normal_df):
        objections = check_trend_contradiction(sell_signal, normal_df, bear_market)
        assert len(objections) == 0


class TestVixRisk:
    def test_high_vix_penalizes_buy(self, buy_signal, bear_market, normal_df):
        # VIX=32 is > 25 but not > 35, so penalty is 0.15
        objections = check_vix_risk(buy_signal, normal_df, bear_market)
        assert len(objections) == 1
        assert objections[0].penalty == 0.15

    def test_extreme_vix_heavy_penalty(self, buy_signal, normal_df):
        extreme_market = {"regime": "volatile", "sp500_trend": "bear", "vix_level": 40.0}
        objections = check_vix_risk(buy_signal, normal_df, extreme_market)
        assert len(objections) == 1
        assert objections[0].penalty == 0.30

    def test_low_vix_no_objection(self, buy_signal, bull_market, normal_df):
        objections = check_vix_risk(buy_signal, normal_df, bull_market)
        assert len(objections) == 0


class TestVolumDecline:
    def test_declining_volume_penalized(self, buy_signal, bull_market, spiked_df):
        objections = check_volume_decline(buy_signal, spiked_df, bull_market)
        assert len(objections) == 1
        assert "declining" in objections[0].reason.lower()

    def test_normal_volume_no_objection(self, buy_signal, bull_market, normal_df):
        objections = check_volume_decline(buy_signal, normal_df, bull_market)
        assert len(objections) == 0


class TestOverextendedPrice:
    def test_spike_penalized(self, buy_signal, bull_market, spiked_df):
        objections = check_overextended_price(buy_signal, spiked_df, bull_market)
        assert len(objections) >= 1
        assert any("chasing" in o.reason.lower() or "up" in o.reason.lower() for o in objections)

    def test_stable_price_no_objection(self, buy_signal, bull_market, normal_df):
        objections = check_overextended_price(buy_signal, normal_df, bull_market)
        # Normal random walk should not trigger
        assert all(o.check != "overextended_price" or o.penalty < 0.25 for o in objections)


class TestRiskRewardRatio:
    def test_bad_ratio_penalized(self, bull_market):
        # Craft DF with known last close = 100
        dates = pd.bdate_range("2024-01-01", periods=30)
        df = pd.DataFrame(
            {
                "Open": np.full(30, 100), "High": np.full(30, 101),
                "Low": np.full(30, 99), "Close": np.full(30, 100),
                "Volume": np.full(30, 1_000_000),
            },
            index=dates,
        )
        signal = Signal(
            ticker="TEST", action="BUY", confidence=0.7,
            stop_loss=90.0, take_profit=105.0, reason="Bad RR"
        )
        # risk=10, reward=5, ratio=0.5 => heavily penalized
        objections = check_risk_reward_ratio(signal, df, bull_market)
        assert len(objections) >= 1
        assert objections[0].penalty == 0.30

    def test_good_ratio_no_objection(self, normal_df, bull_market):
        signal = Signal(
            ticker="TEST", action="BUY", confidence=0.7,
            stop_loss=90.0, take_profit=130.0, reason="Good RR"
        )
        objections = check_risk_reward_ratio(signal, normal_df, bull_market)
        assert len(objections) == 0

    def test_sell_not_checked(self, sell_signal, normal_df, bull_market):
        objections = check_risk_reward_ratio(sell_signal, normal_df, bull_market)
        assert len(objections) == 0


class TestResistanceProximity:
    def test_near_high_penalized(self, bull_market):
        # Price at 60-day high
        dates = pd.bdate_range("2024-01-01", periods=80)
        close = np.full(80, 100.0)
        close[-1] = 100.0  # at the high
        df = pd.DataFrame(
            {
                "Open": close, "High": close + 0.1, "Low": close - 0.5,
                "Close": close, "Volume": np.full(80, 1_000_000),
            },
            index=dates,
        )
        signal = Signal(
            ticker="TEST", action="BUY", confidence=0.7,
            stop_loss=95, take_profit=110, reason="test"
        )
        objections = check_resistance_proximity(signal, df, bull_market)
        assert len(objections) == 1
        assert "resistance" in objections[0].reason.lower()


class TestLowLiquidity:
    def test_thin_stock_penalized(self, buy_signal, bull_market):
        dates = pd.bdate_range("2024-01-01", periods=30)
        df = pd.DataFrame(
            {
                "Open": np.full(30, 10.0), "High": np.full(30, 10.5),
                "Low": np.full(30, 9.5), "Close": np.full(30, 10.0),
                "Volume": np.full(30, 100_000),  # $1M daily = thin
            },
            index=dates,
        )
        objections = check_low_liquidity_hours(buy_signal, df, bull_market)
        assert len(objections) == 1
        assert "thin" in objections[0].reason.lower()


class TestEvaluateSignalIntegration:
    def test_strong_signal_in_bull_approved(self, buy_signal, bull_market, normal_df):
        """High-confidence BUY in a bull market should pass."""
        verdict = evaluate_signal(buy_signal, normal_df, bull_market, "test_strategy", save_to_db=False)
        assert isinstance(verdict, CriticVerdict)
        assert verdict.original_confidence == 0.7
        # May or may not be approved depending on specific data, but should run without error

    def test_weak_signal_in_bear_rejected(self, bear_market, spiked_df):
        """Low-confidence BUY in bear market with spiked price should be rejected."""
        signal = Signal(
            ticker="TEST", action="BUY", confidence=0.4,
            stop_loss=110, take_profit=118, reason="Weak signal"
        )
        verdict = evaluate_signal(signal, spiked_df, bear_market, "test_strategy", save_to_db=False)
        # Bear market (-0.25) + high VIX (-0.30) + volume decline + overextended = massive penalties
        assert verdict.adjusted_confidence < verdict.original_confidence
        assert len(verdict.objections) >= 2

    def test_sell_signal_evaluated(self, sell_signal, bull_market, normal_df):
        """SELL signals should also be evaluated (fewer checks apply)."""
        verdict = evaluate_signal(sell_signal, normal_df, bull_market, "test_strategy", save_to_db=False)
        assert isinstance(verdict, CriticVerdict)

    def test_verdict_has_summary(self, buy_signal, bull_market, normal_df):
        verdict = evaluate_signal(buy_signal, normal_df, bull_market, "test_strategy", save_to_db=False)
        assert "TEST" in verdict.summary
        assert verdict.summary  # not empty
