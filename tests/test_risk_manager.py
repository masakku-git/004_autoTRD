"""Test risk manager."""
import pytest

import src.risk.manager as risk_manager
from config.settings import settings
from src.broker.account import AccountInfo
from src.risk.manager import approve_trade, check_daily_loss_limit
from src.strategy.base import Signal


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """実行環境の .env / DB に依存しないよう、テストの前提条件を固定する。

    - 各テストの期待値は max_positions=3, リスク2%/trade を前提に書かれているため、
      .env の値に関わらずここで固定する
    - approve_trade は ignored_tickers 設定がある環境で trade_log の OPEN 行を
      DB照会する（_open_trade_tickers）ため、DBなしでも動くようスタブする
    """
    monkeypatch.setattr(settings, "max_positions", 3)
    monkeypatch.setattr(settings, "risk_per_trade_pct", 0.02)
    monkeypatch.setattr(settings, "max_portfolio_exposure_pct", 0.90)
    monkeypatch.setattr(settings, "daily_loss_limit_pct", 0.03)
    monkeypatch.setattr(settings, "market_order_cash_reserve_pct", 0.18)
    monkeypatch.setattr(settings, "ignored_tickers", [])
    monkeypatch.setattr(risk_manager, "_open_trade_tickers", lambda: set())


@pytest.fixture
def account():
    return AccountInfo(
        total_equity=3300.0,
        cash=3300.0,
        market_value=0.0,
        positions=[],
    )


@pytest.fixture
def account_full():
    return AccountInfo(
        total_equity=3300.0,
        cash=500.0,
        market_value=2800.0,
        positions=[
            {"ticker": "AAPL", "qty": 5, "avg_price": 180, "market_value": 900, "pnl": 0},
            {"ticker": "MSFT", "qty": 3, "avg_price": 400, "market_value": 1200, "pnl": 0},
            {"ticker": "GOOGL", "qty": 4, "avg_price": 175, "market_value": 700, "pnl": 0},
        ],
    )


@pytest.fixture
def buy_signal():
    return Signal(
        ticker="NVDA",
        action="BUY",
        confidence=0.7,
        stop_loss=90.0,
        take_profit=110.0,
        reason="Test signal",
    )


@pytest.fixture
def sell_signal():
    return Signal(
        ticker="AAPL",
        action="SELL",
        confidence=0.6,
        stop_loss=200.0,
        take_profit=170.0,
        reason="Exit signal",
    )


class TestApproveTrade:
    def test_sell_always_approved(self, sell_signal, account):
        result = approve_trade(sell_signal, account)
        assert result.approved is True

    def test_buy_approved_with_cash(self, buy_signal, account):
        result = approve_trade(buy_signal, account)
        assert result.approved is True
        assert result.quantity > 0

    def test_buy_rejected_max_positions(self, buy_signal, account_full):
        result = approve_trade(buy_signal, account_full)
        assert result.approved is False
        assert "Max positions" in result.reason

    def test_buy_rejected_no_stop_loss(self, account):
        signal = Signal(
            ticker="TEST", action="BUY", confidence=0.5,
            stop_loss=0, take_profit=100, reason="No SL"
        )
        result = approve_trade(signal, account)
        assert result.approved is False

    def test_position_sizing(self, buy_signal, account):
        result = approve_trade(buy_signal, account)
        # risk = 3300 * 0.02 = 66
        # risk_per_share = |100 - 90| = 10
        # quantity = 66 / 10 = 6
        assert result.quantity == 6


class TestDailyLossLimit:
    def test_no_breach(self, account):
        assert check_daily_loss_limit(account, 3300.0) is False

    def test_breach(self):
        account = AccountInfo(total_equity=3150.0, cash=3150.0, market_value=0, positions=[])
        # 3150/3300 - 1 = -4.5%, limit is -3%
        assert check_daily_loss_limit(account, 3300.0) is True

    def test_no_previous(self, account):
        assert check_daily_loss_limit(account, 0.0) is False


def _buy_signal(price: float, stop: float) -> Signal:
    return Signal(
        ticker="XYZ", action="BUY", confidence=0.8,
        stop_loss=stop, take_profit=price * 1.1, reason="t", price=price,
    )


def test_cash_check_includes_market_order_reserve():
    """現金は「現在値×(1+上乗せ率)」で判定する。

    2026-09-11: 現金$1,228.73 に対し VZ($649.61)承認後の残$579.12 で WFC($536.70)を承認したが、
    成行の拘束は約15〜17%増しのためブローカーが余力不足で拒否した。
    """
    acct = AccountInfo(total_equity=3300.0, cash=579.12, market_value=1500.0, positions=[])
    sig = _buy_signal(price=89.45, stop=80.0)  # リスク$9.45/株 → 数量は現金制約が支配
    res = approve_trade(sig, acct)
    assert res.approved
    # 上乗せなしなら int(579.12/89.45)=6 株だが、拘束 89.45*1.18=105.55/株 → 5 株
    assert res.quantity == 5
    assert res.quantity * 89.45 * 1.18 <= 579.12
    assert res.reduced_from is not None and res.reduced_from > res.quantity


def test_cash_check_rejects_when_reserve_makes_one_share_unaffordable():
    acct = AccountInfo(total_equity=3300.0, cash=100.0, market_value=1000.0, positions=[])
    res = approve_trade(_buy_signal(price=90.0, stop=80.0), acct)  # 90*1.18=106.2 > 100
    assert not res.approved
    assert "Insufficient cash" in res.reason


def test_no_reduction_when_cash_is_ample(account):
    res = approve_trade(_buy_signal(price=100.0, stop=95.0), account)
    assert res.approved
    assert res.reduced_from is None


# ---------------------------------------------------------------------------
# ドローダウン歯止め / 未約定買いの反映
# ---------------------------------------------------------------------------
from src.risk.manager import check_drawdown, register_pending_buy  # noqa: E402


@pytest.fixture
def dd_settings(monkeypatch):
    monkeypatch.setattr(settings, "drawdown_halt_pct", 0.08)
    monkeypatch.setattr(settings, "drawdown_alert_pct", 0.12)


def test_drawdown_levels(dd_settings):
    assert check_drawdown(3300.0, 3300.0)[0] == "ok"
    assert check_drawdown(3300.0 * 1.01, 3300.0)[0] == "ok"       # ピーク超え
    assert check_drawdown(3300.0 * 0.93, 3300.0)[0] == "ok"       # -7%
    assert check_drawdown(3300.0 * 0.91, 3300.0)[0] == "halt"     # -9%
    assert check_drawdown(3300.0 * 0.87, 3300.0)[0] == "severe"   # -13%


def test_drawdown_disabled_and_no_peak(monkeypatch):
    monkeypatch.setattr(settings, "drawdown_halt_pct", 0.0)
    monkeypatch.setattr(settings, "drawdown_alert_pct", 0.0)
    assert check_drawdown(1000.0, 3300.0)[0] == "ok"
    assert check_drawdown(1000.0, 0.0)[0] == "ok"


def test_pending_buys_consume_exposure_and_position_slots():
    """同じ実行内の後続の買いが、発注済みの買いを考慮して判定される。

    2026-09-11: 時価$2,118・上限$3,012(90%)の状態で VZ($650)・V($367) を続けて承認し、
    エクスポージャが94%に達した。発注済み分を market_value に反映すれば V は縮小される。
    """
    acct = AccountInfo(
        total_equity=3346.52, cash=1228.73, market_value=2117.79,
        positions=[{"ticker": f"US.T{i}", "qty": 1} for i in range(2)],
    )
    first = approve_trade(_buy_signal(price=49.97, stop=45.0), acct)
    assert first.approved
    register_pending_buy(acct, "VZ", first.quantity * 49.97)
    assert acct.market_value > 2117.79 and len(acct.positions) == 3

    second = approve_trade(_buy_signal(price=367.21, stop=340.0), acct)
    max_investment = 3346.52 * 0.90
    if second.approved:
        assert acct.market_value + second.quantity * 367.21 <= max_investment + 1e-6


def test_pending_buy_same_ticker_does_not_add_position_slot():
    acct = AccountInfo(total_equity=3000.0, cash=1000.0, market_value=500.0,
                       positions=[{"ticker": "US.KO", "qty": 3}])
    register_pending_buy(acct, "KO", 100.0)
    assert len(acct.positions) == 1
    assert acct.market_value == 600.0
