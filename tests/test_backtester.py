"""Test backtest engine."""
import pytest

from src.backtest.engine import run_backtest
from src.strategy.registry import discover_strategies, get_strategy


@pytest.fixture
def strategy():
    """registryに登録された最新版のsma_crossoverを使う（旧版クラスを直接importしない）。"""
    discover_strategies()
    return get_strategy("sma_crossover")


class TestBacktestEngine:
    def test_runs_without_error(self, sample_ohlcv, strategy):
        stats = run_backtest(strategy, "TEST", sample_ohlcv)
        assert stats.num_trades >= 0
        assert -100 <= stats.max_drawdown <= 0
        assert 0 <= stats.win_rate <= 100

    def test_returns_stats_fields(self, sample_ohlcv, strategy):
        stats = run_backtest(strategy, "TEST", sample_ohlcv)
        assert hasattr(stats, "total_return")
        assert hasattr(stats, "sharpe_ratio")
        assert hasattr(stats, "max_drawdown")
        assert hasattr(stats, "win_rate")
        assert hasattr(stats, "num_trades")
        assert hasattr(stats, "trades")
        assert isinstance(stats.trades, list)
