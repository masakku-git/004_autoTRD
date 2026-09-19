"""breakout_v7: 売買ロジックは v6 と完全一致し、診断ログだけが増えること"""
import numpy as np
import pandas as pd
import pytest

from src.strategy.plugins.breakout_v6 import BreakoutV6
from src.strategy.plugins.breakout_v7 import BreakoutV7
from src.strategy.registry import discover_strategies, get_strategy

BULL = {"regime": "trending", "sp500_trend": "bull", "vix_level": 16.0}


def _df(n=250, seed=0, last_close=None, last_vol_mult=1.0, drift=0.001):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(drift, 0.01, n))
    if last_close is not None:
        close[-1] = last_close
    high, low = close * 1.005, close * 0.995
    vol = np.full(n, 1_000_000.0)
    vol[-1] *= last_vol_mult
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close, "Volume": vol}, index=idx)


def test_v7_is_registered_and_replaces_v6():
    discover_strategies()
    s = get_strategy("breakout")
    assert type(s).__name__ == "BreakoutV7" and s.version == "7.0"


@pytest.mark.parametrize("seed", range(40))
def test_signals_identical_to_v6(seed):
    df = _df(seed=seed, last_close=None)
    # 高値ブレイク+出来高増を含む様々な末尾を試す
    for close_mult, vol_mult in [(1.0, 1.0), (1.05, 2.0), (1.05, 1.0), (1.0, 2.0), (0.9, 2.0)]:
        d = df.copy()
        d.iloc[-1, d.columns.get_loc("Close")] = d["Close"].iloc[-2] * close_mult
        d.iloc[-1, d.columns.get_loc("High")] = d["Close"].iloc[-1] * 1.005
        d.iloc[-1, d.columns.get_loc("Volume")] = 1_000_000 * vol_mult
        a, b = BreakoutV6().generate_signals("T", d, BULL), BreakoutV7().generate_signals("T", d, BULL)
        assert (a is None) == (b is None)
        if a is not None:
            assert (a.action, a.stop_loss, a.take_profit, a.take_profit_1, a.confidence) == \
                   (b.action, b.stop_loss, b.take_profit, b.take_profit_1, b.confidence)


def _spike(price_mult, vol_mult):
    d = _df(seed=3)
    prev = d["High"].iloc[-21:-1].max()
    d.iloc[-1, d.columns.get_loc("Close")] = prev * price_mult
    d.iloc[-1, d.columns.get_loc("High")] = prev * price_mult * 1.002
    d.iloc[-1, d.columns.get_loc("Volume")] = 1_000_000 * vol_mult
    return d


def test_reasons_match_outcome():
    s = BreakoutV7()
    assert s.diagnose_entry(_spike(1.02, 2.0), BULL)["reason"] == "buy"
    assert s.diagnose_entry(_spike(1.02, 1.0), BULL)["reason"] == "volume_low"
    r = s.diagnose_entry(_spike(0.99, 2.0), BULL)
    assert r["reason"] == "below_20d_high" and r["metrics"]["gap_pct"] > 0
    assert s.diagnose_entry(_spike(0.99, 1.0), BULL)["reason"] == "no_breakout"


def test_filters_precede_breakout_check():
    s = BreakoutV7()
    assert s.diagnose_entry(_spike(1.02, 2.0), {"sp500_trend": "bear"})["reason"] == "bear_market"
    assert s.diagnose_entry(_df(n=10), BULL)["reason"] == "too_little_data"
    falling = _df(seed=1, drift=-0.004)
    assert s.diagnose_entry(falling, BULL)["reason"] == "below_sma200"


@pytest.mark.parametrize("seed", range(30))
def test_buy_reason_iff_buy_signal(seed):
    s = BreakoutV7()
    d = _spike(1.02, 2.0) if seed % 2 else _spike(0.99, 1.0)
    sig = s.generate_signals("T", d, BULL)
    assert (s.diagnose_entry(d, BULL)["reason"] == "buy") == (sig is not None and sig.action == "BUY")


def test_diagnostic_failure_does_not_break_signals(monkeypatch):
    s = BreakoutV7()
    monkeypatch.setattr(s, "diagnose_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    d = _spike(1.02, 2.0)
    sig = s.generate_signals("T", d, BULL)
    assert sig is not None and sig.action == "BUY"


def test_diag_line_is_logged(caplog):
    import logging
    with caplog.at_level(logging.DEBUG, logger="autotrd"):
        BreakoutV7().generate_signals("ZZZ", _spike(1.02, 1.0), BULL)
    assert any("BREAKOUT-DIAG ticker=ZZZ result=skip reason=volume_low" in r.getMessage() for r in caplog.records)
