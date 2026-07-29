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
