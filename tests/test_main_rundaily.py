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
    captured = {"notifications": [], "orders": [], "closes": [], "partial_closes": []}

    monkeypatch.setattr(main, "is_us_market_day", lambda: True)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "discover_strategies", lambda: 1)
    monkeypatch.setattr(main, "_get_previous_equity", lambda: 0.0)
    monkeypatch.setattr(main, "_update_highest_price", lambda t, h: None)
    monkeypatch.setattr(main, "_get_open_trade_info", lambda t: None)
    monkeypatch.setattr(main, "_get_open_trades", lambda t: [])
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
    monkeypatch.setattr(
        main, "close_trade_log_by_id",
        lambda trade_id, order, price, sold_qty=None, **kw: captured["closes"].append(
            (trade_id, sold_qty)
        ),
    )
    monkeypatch.setattr(
        main, "partial_close_trade_log_by_id",
        lambda trade_id, order, price, sold_qty, **kw: captured["partial_closes"].append(
            (trade_id, sold_qty)
        ),
    )
    monkeypatch.setattr(main, "_get_open_qty", lambda t: 0)
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
        main, "_get_open_trades",
        lambda t: [
            {
                "id": 1,
                "stop_loss": 60.0, "take_profit": 0, "take_profit_1": 0,
                "max_hold_days": 20, "entry_date": date.today() - timedelta(days=2),
                "entry_price": 65.0, "highest_price": 65.0,
                "quantity": 5, "strategy_name": "missing_strategy",
            }
        ],
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


def _lot(lot_id: int, qty: int, days_ago: int, **overrides) -> dict:
    lot = {
        "id": lot_id,
        "stop_loss": 0.0, "take_profit": 0.0, "take_profit_1": 0.0,
        "max_hold_days": 20, "entry_date": date.today() - timedelta(days=days_ago),
        "entry_price": 45.0, "highest_price": 55.0,
        "quantity": qty, "strategy_name": "missing_strategy",
    }
    lot.update(overrides)
    return lot


def test_each_lot_exits_on_its_own_sl_tp1(patched, monkeypatch):
    """同一銘柄の複数ロットが、それぞれのSL/TP1で独立に決済される。"""
    account = FakeAccount(positions=[{"ticker": "US.DDD", "qty": 10}])
    monkeypatch.setattr(main, "get_account_info", lambda: account)
    monkeypatch.setattr(main, "get_ohlcv", lambda t, **kw: _ohlcv(close=50.0))
    monkeypatch.setattr(
        main, "_get_open_trades",
        lambda t: [
            _lot(1, qty=4, days_ago=5, stop_loss=60.0),    # 現在50 <= SL60 → 全量決済
            _lot(2, qty=6, days_ago=2, take_profit_1=45.0),  # 現在50 >= TP1 45 → 半分決済
        ],
    )

    main.run_daily()

    assert patched["orders"] == [("DDD", "SELL", 4), ("DDD", "SELL", 3)]
    assert patched["closes"] == [(1, 4)]
    assert patched["partial_closes"] == [(2, 3)]


def test_tp1_not_refired_but_target_value_kept(patched, monkeypatch):
    """TP1消化済みロットは再発動しない（take_profit_1 をNULL化せず tp1_hit で判定）。"""
    account = FakeAccount(positions=[{"ticker": "US.HHH", "qty": 6}])
    monkeypatch.setattr(main, "get_account_info", lambda: account)
    monkeypatch.setattr(main, "get_ohlcv", lambda t, **kw: _ohlcv(close=50.0))

    seen = {}

    class ExitAwareStrategy:
        name = "tp1_aware"

        def check_exit(self, ticker, df, trade_info):
            seen["take_profit_1"] = trade_info.get("take_profit_1")
            return None

        def generate_signals(self, ticker, df, mc):
            return None

    monkeypatch.setattr(main, "get_strategy", lambda name: ExitAwareStrategy())
    monkeypatch.setattr(
        main, "_get_open_trades",
        lambda t: [_lot(3, qty=6, days_ago=2, take_profit_1=45.0, tp1_hit=True)],
    )

    main.run_daily()

    assert patched["orders"] == []          # TP1は再発動しない
    assert patched["partial_closes"] == []
    # TP1到達後のみ有効な戦略ロジックが参照できるよう、目標値自体は渡される
    assert seen["take_profit_1"] == 45.0


def test_lot_qty_capped_by_broker_quantity(patched, monkeypatch):
    """broker数量がDBより少ない場合、売却株数もPnL計上も実数量に合わせる。"""
    account = FakeAccount(positions=[{"ticker": "US.EEE", "qty": 5}])
    monkeypatch.setattr(main, "get_account_info", lambda: account)
    monkeypatch.setattr(main, "get_ohlcv", lambda t, **kw: _ohlcv(close=50.0))
    monkeypatch.setattr(
        main, "_get_open_trades",
        lambda t: [_lot(7, qty=10, days_ago=3, stop_loss=60.0)],
    )

    main.run_daily()

    assert patched["orders"] == [("EEE", "SELL", 5)]
    # DB上の10株ではなく、実際に売れた5株だけをCLOSE対象にする
    assert patched["closes"] == [(7, 5)]


def test_held_lot_consumes_broker_quantity(patched, monkeypatch):
    """決済しないロットもbroker数量を占有し、後続ロットが二重売りしない。"""
    account = FakeAccount(positions=[{"ticker": "US.FFF", "qty": 10}])
    monkeypatch.setattr(main, "get_account_info", lambda: account)
    monkeypatch.setattr(main, "get_ohlcv", lambda t, **kw: _ohlcv(close=50.0))
    monkeypatch.setattr(
        main, "_get_open_trades",
        lambda t: [
            _lot(1, qty=10, days_ago=3),                   # エグジット条件なし＝保有継続
            _lot(2, qty=10, days_ago=1, stop_loss=60.0),   # SL該当だが割り当てる株数が無い
        ],
    )

    main.run_daily()

    assert patched["orders"] == []
    assert patched["closes"] == []


def test_sell_signal_qty_capped_by_open_lots(patched, monkeypatch):
    """Step 3で決済済みのロット分をStep 8のSELLが二重に売らない。"""
    account = FakeAccount(positions=[{"ticker": "US.GGG", "qty": 10}])
    monkeypatch.setattr(main, "get_account_info", lambda: account)
    monkeypatch.setattr(main, "get_ohlcv", lambda t, **kw: _ohlcv(close=50.0))
    monkeypatch.setattr(
        main, "_get_open_trade_info",
        lambda t: _lot(2, qty=6, days_ago=1),
    )
    # Step 3で4株分のロットを決済済み → trade_log上のOPENは6株だけ
    monkeypatch.setattr(main, "_get_open_qty", lambda t: 6)

    class SellStrategy:
        name = "sell_strategy"

        def generate_signals(self, ticker, df, mc):
            return main.Signal(
                ticker=ticker, action="SELL", confidence=1.0,
                stop_loss=0, take_profit=0, reason="test sell", price=50.0,
            )

        def check_exit(self, ticker, df, trade_info):
            return None

    monkeypatch.setattr(main, "get_strategy", lambda name: SellStrategy())
    monkeypatch.setattr(
        main, "evaluate_signal",
        lambda s, df, mc, name: type("V", (), {"approved": True, "adjusted_confidence": 1.0})(),
    )
    monkeypatch.setattr(
        main, "approve_trade",
        lambda s, acc, mc: type("A", (), {"approved": True, "quantity": 0, "reason": ""})(),
    )

    main.run_daily()

    # 口座上の10株ではなく、DB上OPENな6株だけを売る
    assert ("GGG", "SELL", 6) in patched["orders"]
    assert ("GGG", "SELL", 10) not in patched["orders"]


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
