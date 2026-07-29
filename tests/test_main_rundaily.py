"""Integration-style tests for run_daily error isolation.

2026-07-29の障害（スクリーニングのクラッシュで損切りまで止まった）の回帰テスト。
外部依存（DB/moomoo/yfinance/Slack）はすべてmonkeypatchする。
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

import src.main as main


class FakeAccount:
    def __init__(self, positions=None):
        self.total_equity = 10_000.0
        self.cash = 5_000.0
        self.positions = positions or []


class FakeOrder:
    status = "DRY_RUN"


def _ohlcv(close: float, rows: int = 30) -> pd.DataFrame:
    series = pd.Series([close] * rows)
    return pd.DataFrame(
        {
            "Open": series,
            "High": series + 1,
            "Low": series - 1,
            "Close": series,
            "Adj Close": series,
            "Volume": [1_000_000] * rows,
        }
    )


@pytest.fixture
def patched(monkeypatch):
    """Patch every external dependency of run_daily; return the capture dict."""
    captured = {"notifications": [], "orders": []}

    monkeypatch.setattr(main, "is_us_market_day", lambda: True)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "discover_strategies", lambda: 1)
    monkeypatch.setattr(main, "_get_previous_equity", lambda: 0.0)
    monkeypatch.setattr(main, "_update_highest_price", lambda t, h: None)
    monkeypatch.setattr(main, "_get_open_trade_info", lambda t: None)
    monkeypatch.setattr(
        main, "send_notification",
        lambda title, message, level="info": captured["notifications"].append(
            (title, message, level)
        ) or True,
    )
    monkeypatch.setattr(
        main, "assess_market_condition",
        lambda: {"sp500_trend": "bull", "vix_level": 15.0, "regime": "trending",
                 "data_degraded": False},
    )
    monkeypatch.setattr(main, "select_strategies", lambda mc: [])
    monkeypatch.setattr(main, "run_screening", lambda: [])
    monkeypatch.setattr(main, "get_account_info", lambda: FakeAccount())
    monkeypatch.setattr(main, "get_ohlcv", lambda t, **kw: pd.DataFrame())
    monkeypatch.setattr(
        main, "place_order",
        lambda signal, qty, **kw: captured["orders"].append((signal.ticker, signal.action, qty))
        or FakeOrder(),
    )
    monkeypatch.setattr(main, "close_trade_log", lambda *a, **kw: None)
    monkeypatch.setattr(main, "partial_close_trade_log", lambda *a, **kw: None)
    return captured


def test_screening_failure_does_not_abort_run(patched, monkeypatch):
    def boom():
        raise ValueError("screening blew up")

    monkeypatch.setattr(main, "run_screening", boom)

    main.run_daily()  # must not raise

    titles = [t for t, _, _ in patched["notifications"]]
    assert any("スクリーニング失敗" in t for t in titles)
    # 日次レポートは届き、実行時警告セクションを含む
    report = next(m for t, m, _ in patched["notifications"] if "日次" in t)
    assert "実行時警告" in report
    assert "スクリーニング失敗" in report


def test_forced_exit_isolated_per_ticker(patched, monkeypatch):
    """1銘柄のデータ取得失敗が他銘柄の損切りを止めない。"""
    account = FakeAccount(positions=[
        {"ticker": "US.AAA", "qty": 10},
        {"ticker": "US.BBB", "qty": 5},
    ])
    monkeypatch.setattr(main, "get_account_info", lambda: account)

    def fake_ohlcv(ticker, **kw):
        if ticker == "AAA":
            raise RuntimeError("yfinance down")
        return _ohlcv(close=50.0)

    monkeypatch.setattr(main, "get_ohlcv", fake_ohlcv)
    monkeypatch.setattr(
        main, "_get_open_trade_info",
        lambda t: {
            "stop_loss": 60.0, "take_profit": 0, "take_profit_1": 0,
            "max_hold_days": 20, "entry_date": date.today() - timedelta(days=2),
            "entry_price": 65.0, "highest_price": 65.0,
            "quantity": 5, "strategy_name": "missing_strategy",
        },
    )

    main.run_daily()

    # BBBのストップロス(SL=60 > 現在50)は発動している
    assert ("BBB", "SELL", 5) in patched["orders"]
    # AAAの失敗はerror通知に集約されている
    titles = [t for t, _, lv in patched["notifications"] if lv == "error"]
    assert any("損切り判定エラー" in t for t in titles)
    error_msg = next(m for t, m, _ in patched["notifications"] if "損切り判定エラー" in t)
    assert "AAA" in error_msg


def test_nan_price_skips_sl_check_with_notification(patched, monkeypatch):
    """価格NaNでは損切り判定をスキップし、無音のNaN比較(常にFalse)に頼らない。"""
    account = FakeAccount(positions=[{"ticker": "US.CCC", "qty": 3}])
    monkeypatch.setattr(main, "get_account_info", lambda: account)

    df = _ohlcv(close=50.0)
    df.loc[len(df) - 1, "Close"] = np.nan
    monkeypatch.setattr(main, "get_ohlcv", lambda t, **kw: df)

    main.run_daily()

    assert patched["orders"] == []
    titles = [t for t, _, _ in patched["notifications"]]
    assert any("価格データ異常" in t for t in titles)


def test_degraded_market_blocks_new_entries(patched, monkeypatch):
    monkeypatch.setattr(
        main, "assess_market_condition",
        lambda: {"sp500_trend": "neutral", "vix_level": 0.0, "regime": "volatile",
                 "data_degraded": True},
    )

    main.run_daily()

    titles = [t for t, _, _ in patched["notifications"]]
    assert any("市場データ取得失敗" in t for t in titles)
    report = next(m for t, m, _ in patched["notifications"] if "日次" in t)
    assert "実行時警告" in report
