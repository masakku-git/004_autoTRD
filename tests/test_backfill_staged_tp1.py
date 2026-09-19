"""scripts/backfill_staged_tp1.py: 段階利確の未計上分を実約定価格で追加する"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import backfill_staged_tp1 as bf  # noqa: E402
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
        # AMZN: 買い5株@235.38 → TP1で2株売却@249.18（残3株がOPEN行）
        s.add(Order(id=6, ticker="AMZN", side="BUY", order_type="MARKET", quantity=5, status="FILLED",
                    strategy_name="pullback", filled_price=235.38, created_at=datetime(2026, 4, 10, 13)))
        s.add(Order(id=10, ticker="AMZN", side="SELL", order_type="MARKET", quantity=2, status="FILLED",
                    strategy_name="pullback", filled_price=249.18, created_at=datetime(2026, 4, 15, 13)))
        s.add(TradeLog(ticker="AMZN", entry_order_id=6, entry_date=date(2026, 4, 10), entry_price=235.38,
                       quantity=3, strategy_name="pullback", status="OPEN"))
        s.commit()
    monkeypatch.setattr(bf, "PARTIALS", [bf.Partial("AMZN", 10, 6, 2)])


def _rows():
    with get_session() as s:
        return s.execute(select(TradeLog).order_by(TradeLog.id)).scalars().all()


def test_dry_run_changes_nothing():
    assert bf.backfill(apply=False) == pytest.approx(27.6)
    assert len(_rows()) == 1


def test_apply_adds_closed_partial_row_with_actual_fill_pnl():
    bf.backfill(apply=True)
    rows = _rows()
    assert len(rows) == 2
    added = rows[1]
    assert added.status == "CLOSED" and added.tp1_hit is True
    assert added.quantity == 2 and added.exit_price == 249.18
    assert added.pnl == pytest.approx((249.18 - 235.38) * 2)
    assert added.exit_date == date(2026, 4, 15)
    assert rows[0].quantity == 3  # 元のOPEN行は触らない


def test_apply_is_idempotent():
    bf.backfill(apply=True)
    bf.backfill(apply=True)
    assert len(_rows()) == 2


def test_mismatched_ticker_aborts(monkeypatch):
    monkeypatch.setattr(bf, "PARTIALS", [bf.Partial("MSFT", 10, 6, 2)])
    with pytest.raises(RuntimeError):
        bf.backfill(apply=True)
