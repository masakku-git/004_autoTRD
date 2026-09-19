"""check_position_history（DB版）が trade_log から正しく OPEN ロット・損失決済日を読むことの確認"""
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.models.base as models_base
from config.settings import settings
from src.models.base import Base, get_session
from src.models.trade import Order, TradeLog
from src.strategy import critic
from src.strategy.base import Signal
from src.utils.helpers import utcnow


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    eng = create_engine("sqlite://")
    monkeypatch.setattr(models_base, "engine", eng)
    monkeypatch.setattr(models_base, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    Base.metadata.create_all(eng, tables=[Order.__table__, TradeLog.__table__])
    monkeypatch.setattr(settings, "block_averaging_down", True)
    monkeypatch.setattr(settings, "max_open_lots_per_ticker", 3)
    monkeypatch.setattr(settings, "reentry_cooldown_business_days", 5)
    monkeypatch.setattr("src.utils.helpers.today_jst", lambda: date(2026, 9, 1))


def _lot(ticker, status, entry_price, qty=1, pnl=None, exit_date=None):
    with get_session() as s:
        o = Order(ticker=ticker, side="BUY", order_type="MARKET", quantity=qty, status="FILLED",
                  strategy_name="pullback", created_at=utcnow())
        s.add(o)
        s.flush()
        s.add(TradeLog(ticker=ticker, entry_order_id=o.id, entry_date=date(2026, 8, 20),
                       entry_price=entry_price, quantity=qty, strategy_name="pullback",
                       status=status, pnl=pnl, exit_date=exit_date))
        s.commit()


def _df(close):
    return pd.DataFrame({"Close": [close]})


def _sig(ticker="ABBV"):
    return Signal(ticker=ticker, action="BUY", confidence=0.8, stop_loss=1.0, take_profit=999.0, reason="t")


def test_open_lot_underwater_is_rejected():
    _lot("ABBV", "OPEN", 258.67)
    objs = critic.check_position_history(_sig(), _df(254.17), {})
    assert [o.check for o in objs] == ["averaging_down"]


def test_other_tickers_are_ignored():
    _lot("KO", "OPEN", 500.0)
    assert critic.check_position_history(_sig("ABBV"), _df(100.0), {}) == []


def test_recent_loss_triggers_cooldown_but_win_does_not():
    _lot("ABBV", "CLOSED", 250.0, pnl=-10.0, exit_date=date(2026, 8, 27))  # 3営業日前
    assert [o.check for o in critic.check_position_history(_sig(), _df(260.0), {})] == ["reentry_cooldown"]


def test_profitable_close_does_not_trigger_cooldown():
    _lot("ABBV", "CLOSED", 250.0, pnl=+10.0, exit_date=date(2026, 8, 31))
    assert critic.check_position_history(_sig(), _df(260.0), {}) == []
