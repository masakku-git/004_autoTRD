"""Test strategy plugins.

registry経由で「現在有効な最新版」を対象にする（同名戦略は最新版が登録に勝つ）。
旧版のクラスを直接importしない — 版が増えてもテストの書き換えが不要になり、
戦略ファイルの版管理ルール（旧版は変更せず残す）とも整合する。
"""
import pandas as pd
import pytest

from src.strategy import registry
from src.strategy.base import Signal

registry.discover_strategies()
STRATEGY_CLASSES = [registry._registry[name] for name in sorted(registry._registry)]

assert STRATEGY_CLASSES, "戦略プラグインが1件も登録されていません"

_VALID_REGIMES = {"trending", "range", "volatile", "any"}


def _ids(cls):
    return f"{cls.name}_v{cls.version}"


@pytest.mark.parametrize("cls", STRATEGY_CLASSES, ids=_ids)
class TestAllStrategies:
    def test_metadata(self, cls):
        s = cls()
        assert s.name and s.name != "unnamed"
        assert s.version
        assert s.target_regime in _VALID_REGIMES

    def test_returns_none_insufficient_data(self, cls, market_condition_trending):
        s = cls()
        df = pd.DataFrame(
            {"Open": [1], "High": [2], "Low": [0.5], "Close": [1.5], "Volume": [100]},
            index=pd.to_datetime(["2024-01-01"]),
        )
        assert s.generate_signals("TEST", df, market_condition_trending) is None

    def test_generate_signals_does_not_error(self, cls, sample_ohlcv,
                                             market_condition_trending):
        s = cls()
        signal = s.generate_signals("TEST", sample_ohlcv, market_condition_trending)
        # シグナルの有無はデータ次第だが、例外を出さず妥当な値を返すこと
        if signal is not None:
            assert isinstance(signal, Signal)
            assert signal.action in ("BUY", "SELL")
            assert 0 <= signal.confidence <= 1

    def test_get_params_returns_dict(self, cls):
        assert isinstance(cls().get_params(), dict)


def test_registry_has_latest_versions():
    """同名戦略は最新版が登録に勝つ（例: breakoutはv2〜v6のうちv6が有効）。"""
    names = {c.name for c in STRATEGY_CLASSES}
    assert {"breakout", "pullback", "rsi_reversal", "sma_crossover"} <= names
