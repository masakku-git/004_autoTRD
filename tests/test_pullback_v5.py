"""pullback_v5: TP1消化後のトレール幅だけが v4 と異なること"""
import numpy as np
import pandas as pd
import pytest

from src.strategy.plugins.pullback_v4 import PullbackV4
from src.strategy.plugins.pullback_v5 import PullbackV5
from src.strategy.registry import discover_strategies, get_strategy


def _uptrend_then_pullback(n=120, seed=0):
    """上昇して高値をつけ、その後少し押した価格系列（トレーリング判定の対象になる形）"""
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0.4, 1.0, n))
    close[-6:] = close[-7] + np.array([1.5, 2.5, 3.0, 2.0, 1.0, -1.0])
    idx = pd.bdate_range("2026-01-01", periods=n)
    return pd.DataFrame({"Open": close, "High": close * 1.005, "Low": close * 0.995,
                         "Close": close, "Volume": np.full(n, 1e6)}, index=idx)


def test_v5_is_registered_and_replaces_v4():
    discover_strategies()
    s = get_strategy("pullback")
    assert type(s).__name__ == "PullbackV5" and s.version == "5.0"


@pytest.mark.parametrize("seed", range(30))
def test_identical_to_v4_before_tp1(seed):
    df = _uptrend_then_pullback(seed=seed)
    high = float(df["High"].max())
    info = {"entry_price": float(df["Close"].iloc[0]), "highest_price": high, "tp1_hit": False,
            "take_profit": high * 2, "stop_loss": 0, "take_profit_1": high * 2}
    a, b = PullbackV4().check_exit("T", df, info), PullbackV5().check_exit("T", df, info)
    assert (a is None) == (b is None)
    if a is not None:
        assert (a.should_exit, a.reason) == (b.should_exit, b.reason)


def test_signals_identical_to_v4():
    df = _uptrend_then_pullback(n=260, seed=2)
    mc = {"regime": "trending", "sp500_trend": "bull", "vix_level": 16.0}
    a, b = PullbackV4().generate_signals("T", df, mc), PullbackV5().generate_signals("T", df, mc)
    assert (a is None) == (b is None)


def test_trail_is_tightened_after_tp1_only(monkeypatch):
    """ADX連動の幅が2.0×ATRのとき、高値からの押しが1.75×ATRなら
    TP1前は幅内で保持(トレールでは決済されない)、TP1後は cap=1.5 に短縮されて決済される。"""
    strat = PullbackV5(tp1_trail_cap=1.5)
    monkeypatch.setattr(strat, "_dynamic_trail_multiplier", lambda df: 2.0)
    df = _uptrend_then_pullback(seed=5)
    price = float(df["Close"].iloc[-1])
    atr = max(strat._calculate_atr(df), price * 0.02)
    info = {"entry_price": price - 6 * atr, "highest_price": price + 1.75 * atr, "tp1_hit": False,
            "take_profit": price * 3, "stop_loss": 0, "take_profit_1": price * 3}

    before = strat.check_exit("T", df, info)
    assert before is None or "Dynamic trailing stop" not in before.reason

    info["tp1_hit"] = True
    after = strat.check_exit("T", df, info)
    assert after is not None and after.should_exit and "TP1後に短縮" in after.reason


def test_cap_is_upper_bound_not_widening():
    """ADX連動の幅が cap より狭い(1.5)場合は、cap を大きくしても広がらない"""
    df = _uptrend_then_pullback(seed=7)
    s = PullbackV5(tp1_trail_cap=3.0)
    assert s.get_params()["tp1_trail_cap"] == 3.0
    info = {"entry_price": 100.0, "highest_price": 130.0, "tp1_hit": True,
            "take_profit": 999, "stop_loss": 0, "take_profit_1": 999}
    a = PullbackV4().check_exit("T", df, info)
    b = s.check_exit("T", df, info)
    assert (a is None) == (b is None)
