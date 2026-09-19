"""scripts/backfill_manual_exits.py: 手動決済で欠けた実績の補正"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import backfill_manual_exits as bf  # noqa: E402
import src.models.base as models_base  # noqa: E402
from src.models.base import Base, get_session  # noqa: E402
from src.models.trade import Order, TradeLog  # noqa: E402


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    eng = create_engine("sqlite://")
    monkeypatch.setattr(models_base, "engine", eng)
    monkeypatch.setattr(models_base, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    Base.metadata.create_all(eng, tables=[Order.__table__, TradeLog.__table__])
    with get_session() as s:
        s.add(Order(id=24, ticker="AMD", side="SELL", order_type="LIMIT", quantity=1, status="FILLED",
                    strategy_name="unknown", filled_price=325.90, created_at=datetime(2026, 4, 29, 13, 1)))
        # id=4: AMZN 3株、決済価格が誤って 249.18、pnl 空欄
        s.add(TradeLog(id=4, ticker="AMZN", entry_order_id=6, exit_order_id=10, entry_date=date(2026, 4, 10),
                       exit_date=date(2026, 4, 30), entry_price=235.38, exit_price=249.18, quantity=3,
                       strategy_name="breakout", status="CLOSED", notes="auto-closed"))
        # id=16: AMD 1株、決済価格は正しく pnl だけ空欄（既存注文24）
        s.add(TradeLog(id=16, ticker="AMD", entry_order_id=22, exit_order_id=24, entry_date=date(2026, 4, 27),
                       exit_date=date(2026, 4, 30), entry_price=347.02, exit_price=325.90, quantity=1,
                       strategy_name="breakout", status="CLOSED", notes="auto-closed"))
        s.commit()
    monkeypatch.setattr(bf, "FIXES", [
        bf.Fix(4, "AMZN", 3, 235.38, 253.61, "FJ1C68693118A1F000", "2026-04-22 13:16:49.064", "2026-04-22 13:23:06.460"),
        bf.Fix(16, "AMD", 1, 347.02, 325.90, existing_exit_order_id=24),
    ])


def _row(i):
    with get_session() as s:
        return s.get(TradeLog, i)


def test_dry_run_changes_nothing():
    assert bf.apply_fixes(apply=False) == pytest.approx((253.61 - 235.38) * 3 + (325.90 - 347.02))
    assert _row(4).pnl is None
    with get_session() as s:
        assert s.execute(select(Order).where(Order.side == "SELL", Order.strategy_name == "manual")).first() is None


def test_apply_fixes_price_date_pnl_and_adds_manual_order():
    bf.apply_fixes(apply=True)
    r4 = _row(4)
    assert r4.exit_price == 253.61 and r4.exit_date == date(2026, 4, 22)
    assert r4.pnl == pytest.approx(54.69)
    assert r4.pnl_pct == pytest.approx((253.61 / 235.38 - 1) * 100)
    with get_session() as s:
        o = s.execute(select(Order).where(Order.broker_order_id == "FJ1C68693118A1F000")).scalar_one()
        assert (o.side, o.status, o.quantity, o.filled_price, o.strategy_name) == ("SELL", "FILLED", 3, 253.61, "manual")
        assert o.commission is None  # 次回の reconcile_fills が取得する
        # 米国東部 13:16:49 (EDT=UTC-4) → UTC 17:16:49
        assert o.created_at.hour == 17 and o.created_at.date() == date(2026, 4, 22)
    assert r4.exit_order_id == o.id
    r16 = _row(16)
    assert r16.pnl == pytest.approx(-21.12) and r16.exit_order_id == 24
    assert r16.exit_date == date(2026, 4, 29)


def test_apply_is_idempotent():
    bf.apply_fixes(apply=True)
    bf.apply_fixes(apply=True)
    with get_session() as s:
        assert len(s.execute(select(Order).where(Order.strategy_name == "manual")).all()) == 1


def test_unexpected_state_aborts_without_changes(monkeypatch):
    monkeypatch.setattr(bf, "FIXES", [
        bf.Fix(4, "AMZN", 3, 999.0, 253.61, "FJ1C68693118A1F000", "2026-04-22 13:16:49.064", "2026-04-22 13:23:06.460"),
    ])
    with pytest.raises(RuntimeError):
        bf.apply_fixes(apply=True)
    assert _row(4).pnl is None
