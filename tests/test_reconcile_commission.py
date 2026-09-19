"""reconcile_fills.sync_commissions: 約定済み注文への手数料の記録"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import reconcile_fills  # noqa: E402
import src.models.base as models_base  # noqa: E402
from src.models.base import Base, get_session  # noqa: E402
from src.models.trade import Order, TradeLog  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    eng = create_engine("sqlite://")
    monkeypatch.setattr(models_base, "engine", eng)
    monkeypatch.setattr(models_base, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    Base.metadata.create_all(eng, tables=[Order.__table__, TradeLog.__table__])


def _order(broker_id, status="FILLED", commission=None, ticker="KO"):
    with get_session() as s:
        o = Order(ticker=ticker, side="BUY", order_type="MARKET", quantity=1, status=status,
                  broker_order_id=broker_id, strategy_name="pullback", commission=commission)
        s.add(o)
        s.commit()


def _commissions():
    with get_session() as s:
        return {o.broker_order_id: o.commission for o in s.execute(select(Order)).scalars()}


def test_records_fee_only_for_filled_orders_without_commission():
    _order("A")                        # 対象
    _order("B", commission=0.5)        # 取得済み → 触らない
    _order("C", status="SUBMITTED")    # 未約定 → 対象外
    _order("D", status="FAILED")       # 失敗 → 対象外
    asked = []

    def fake_fetch(ids):
        asked.extend(ids)
        return {i: 0.86 for i in ids}

    assert reconcile_fills.sync_commissions(dry_run=False, fetch=fake_fetch) == 1
    assert asked == ["A"]
    assert _commissions() == {"A": 0.86, "B": 0.5, "C": None, "D": None}


def test_zero_fee_is_recorded_as_zero_not_null():
    """手数料0（キャンペーン等）は NULL(未取得)と区別して 0.0 で残す"""
    _order("A")
    reconcile_fills.sync_commissions(dry_run=False, fetch=lambda ids: {"A": 0.0})
    assert _commissions()["A"] == 0.0


def test_missing_fee_stays_null_for_retry_next_run():
    _order("A")
    _order("B")
    reconcile_fills.sync_commissions(dry_run=False, fetch=lambda ids: {"A": 1.0})
    assert _commissions() == {"A": 1.0, "B": None}


def test_dry_run_does_not_write():
    _order("A")
    assert reconcile_fills.sync_commissions(dry_run=True, fetch=lambda ids: {"A": 1.0}) == 1
    assert _commissions()["A"] is None
