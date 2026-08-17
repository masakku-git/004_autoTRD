"""executor_v2.partial_close_trade_log の分割記録テスト"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.models.base as models_base  # noqa: E402
from src.models.base import Base, get_session  # noqa: E402
from src.models.trade import Order, TradeLog  # noqa: E402
from src.broker.executor_v2 import (  # noqa: E402
    close_trade_log_by_id,
    partial_close_trade_log,
    partial_close_trade_log_by_id,
)
from src.utils.helpers import utcnow  # noqa: E402


# critic_evaluations 等が PostgreSQL 専用型 (JSONB) を使うため、
# SQLite では必要なテーブルのみ作成する
_TABLES = [Order.__table__, TradeLog.__table__]


@pytest.fixture(autouse=True)
def _fresh_db(monkeypatch):
    """本番DB設定に依存しないよう、インメモリSQLiteにエンジンを差し替える"""
    eng = create_engine("sqlite://")
    monkeypatch.setattr(models_base, "engine", eng)
    monkeypatch.setattr(models_base, "SessionLocal",
                        sessionmaker(bind=eng, expire_on_commit=False))
    Base.metadata.create_all(eng, tables=_TABLES)
    yield


def _make_order(ticker: str, qty: int, filled_price: float) -> Order:
    return Order(
        ticker=ticker,
        side="SELL",
        order_type="LIMIT",
        quantity=qty,
        price=filled_price,
        status="FILLED",
        filled_price=filled_price,
        filled_at=utcnow(),
        strategy_name="breakout",
    )


def _make_open_trade(ticker: str, qty: int, entry_price: float,
                     entry_order_id: int, entry_date=date(2026, 7, 1)) -> TradeLog:
    return TradeLog(
        ticker=ticker,
        entry_order_id=entry_order_id,
        entry_date=entry_date,
        entry_price=entry_price,
        quantity=qty,
        strategy_name="breakout",
        stop_loss=90.0,
        take_profit=130.0,
        take_profit_1=110.0,
        max_hold_days=60,
        status="OPEN",
    )


def test_partial_close_records_pnl_in_split_row():
    """TP1で5株中2株を売却 → 売却分がCLOSED行としてpnl付きで記録される"""
    with get_session() as session:
        entry = Order(ticker="AMZN", side="BUY", order_type="LIMIT", quantity=5,
                      price=100.0, status="FILLED", filled_price=100.0,
                      strategy_name="breakout")
        session.add(entry)
        session.flush()
        session.add(_make_open_trade("AMZN", 5, 100.0, entry.id))
        exit_order = _make_order("AMZN", 2, 120.0)
        session.add(exit_order)
        session.commit()
        exit_order_id = exit_order.id

    partial_close_trade_log("AMZN", exit_order, 120.0, 2)

    with get_session() as session:
        rows = session.query(TradeLog).order_by(TradeLog.id).all()
        assert len(rows) == 2

        open_row = [r for r in rows if r.status == "OPEN"][0]
        closed_row = [r for r in rows if r.status == "CLOSED"][0]

        # 売却2株分が独立したCLOSED行になり、pnlが数値として入る
        assert closed_row.quantity == 2
        assert closed_row.pnl == pytest.approx((120.0 - 100.0) * 2)
        assert closed_row.pnl_pct == pytest.approx(20.0)
        assert closed_row.exit_price == pytest.approx(120.0)
        assert closed_row.exit_order_id == exit_order_id
        assert closed_row.strategy_name == "breakout"

        # 元の行は残3株でOPEN継続、TP1は消化済みフラグで記録（値は残す）
        assert open_row.quantity == 3
        assert open_row.pnl is None
        assert open_row.tp1_hit is True
        assert open_row.take_profit_1 == pytest.approx(110.0)

        # 集計: pnl列の合計に段階利確分が反映される
        total = sum(r.pnl or 0 for r in rows)
        assert total == pytest.approx(40.0)


def test_partial_close_full_row_consumption_unchanged():
    """1株行×3のうち1株売却 → 最古の行が丸ごとCLOSEDになる従来動作は維持"""
    with get_session() as session:
        entry = Order(ticker="AMGN", side="BUY", order_type="LIMIT", quantity=3,
                      price=350.0, status="FILLED", filled_price=350.0,
                      strategy_name="pullback")
        session.add(entry)
        session.flush()
        session.add(_make_open_trade("AMGN", 1, 348.0, entry.id, date(2026, 6, 23)))
        session.add(_make_open_trade("AMGN", 1, 350.0, entry.id, date(2026, 6, 24)))
        session.add(_make_open_trade("AMGN", 1, 352.0, entry.id, date(2026, 6, 25)))
        exit_order = _make_order("AMGN", 1, 374.0)
        session.add(exit_order)
        session.commit()

    partial_close_trade_log("AMGN", exit_order, 374.0, 1)

    with get_session() as session:
        rows = session.query(TradeLog).order_by(TradeLog.entry_date).all()
        assert len(rows) == 3  # 分割行は増えない（行を丸ごと消化）
        assert rows[0].status == "CLOSED"
        assert rows[0].pnl == pytest.approx(374.0 - 348.0)
        # 残り2行はOPENのまま、TP1は消化済みフラグで記録（値は残す）
        for r in rows[1:]:
            assert r.status == "OPEN"
            assert r.tp1_hit is True
            assert r.take_profit_1 == pytest.approx(110.0)


def test_partial_close_fifo_across_rows_with_split():
    """複数OPEN行をFIFOで跨いで消化：先頭行は全消化、次行は分割"""
    with get_session() as session:
        entry = Order(ticker="INTC", side="BUY", order_type="LIMIT", quantity=7,
                      price=100.0, status="FILLED", filled_price=100.0,
                      strategy_name="breakout")
        session.add(entry)
        session.flush()
        session.add(_make_open_trade("INTC", 2, 100.0, entry.id, date(2026, 7, 1)))
        session.add(_make_open_trade("INTC", 5, 105.0, entry.id, date(2026, 7, 2)))
        exit_order = _make_order("INTC", 4, 115.0)
        session.add(exit_order)
        session.commit()

    partial_close_trade_log("INTC", exit_order, 115.0, 4)

    with get_session() as session:
        rows = session.query(TradeLog).all()
        closed = sorted([r for r in rows if r.status == "CLOSED"], key=lambda r: r.quantity)
        open_rows = [r for r in rows if r.status == "OPEN"]

        assert len(closed) == 2 and len(open_rows) == 1
        # 行1（2株@100）全消化 + 行2から2株@105を分割
        assert {c.quantity for c in closed} == {2}
        total_closed_pnl = sum(c.pnl for c in closed)
        assert total_closed_pnl == pytest.approx((115 - 100) * 2 + (115 - 105) * 2)
        # 行2の残り3株はOPEN
        assert open_rows[0].quantity == 3
        assert open_rows[0].pnl is None


# --- ロット単位クローズ (close_trade_log_by_id / partial_close_trade_log_by_id) ---


def _seed_two_lots(ticker: str) -> tuple[int, int, Order]:
    """同一銘柄の2ロット（3株/5株）と売り注文を作り、(lot1_id, lot2_id, exit_order)を返す"""
    with get_session() as session:
        entry = Order(ticker=ticker, side="BUY", order_type="LIMIT", quantity=8,
                      price=100.0, status="FILLED", filled_price=100.0,
                      strategy_name="breakout")
        session.add(entry)
        session.flush()
        lot1 = _make_open_trade(ticker, 3, 100.0, entry.id, date(2026, 7, 1))
        lot2 = _make_open_trade(ticker, 5, 108.0, entry.id, date(2026, 7, 5))
        session.add_all([lot1, lot2])
        exit_order = _make_order(ticker, 3, 120.0)
        session.add(exit_order)
        session.commit()
        return lot1.id, lot2.id, exit_order


def test_close_by_id_touches_only_that_lot():
    lot1_id, lot2_id, exit_order = _seed_two_lots("NVDA")

    close_trade_log_by_id(lot1_id, exit_order, 120.0, sold_qty=3)

    with get_session() as session:
        lot1 = session.get(TradeLog, lot1_id)
        lot2 = session.get(TradeLog, lot2_id)
        assert lot1.status == "CLOSED"
        assert lot1.pnl == pytest.approx((120.0 - 100.0) * 3)
        assert lot1.notes and "全量決済" in lot1.notes
        # もう一方のロットは無傷（TP1も残る）
        assert lot2.status == "OPEN"
        assert lot2.quantity == 5
        assert lot2.take_profit_1 == pytest.approx(110.0)


def test_close_by_id_with_short_qty_splits_and_keeps_rest_open():
    """broker数量がDBより少ない場合、売れた分だけをPnL計上し残りはOPENで残す"""
    lot1_id, _, exit_order = _seed_two_lots("AMD")

    close_trade_log_by_id(lot1_id, exit_order, 120.0, sold_qty=1)

    with get_session() as session:
        lot1 = session.get(TradeLog, lot1_id)
        split = session.query(TradeLog).filter(
            TradeLog.status == "CLOSED", TradeLog.ticker == "AMD"
        ).one()
        # 3株分ではなく実際に売れた1株分だけがPnLになる
        assert split.quantity == 1
        assert split.pnl == pytest.approx(120.0 - 100.0)
        # 元のロットは残2株でOPEN継続、SL/TP判定用のTP1も維持
        assert lot1.status == "OPEN"
        assert lot1.quantity == 2
        assert lot1.take_profit_1 == pytest.approx(110.0)


def test_partial_close_by_id_consumes_only_its_own_tp1():
    lot1_id, lot2_id, exit_order = _seed_two_lots("MSFT")

    partial_close_trade_log_by_id(lot1_id, exit_order, 120.0, 1)

    with get_session() as session:
        lot1 = session.get(TradeLog, lot1_id)
        lot2 = session.get(TradeLog, lot2_id)
        assert lot1.quantity == 2 and lot1.status == "OPEN"
        assert lot1.tp1_hit is True                        # 自ロットのTP1のみ消費
        assert lot1.take_profit_1 == pytest.approx(110.0)  # 値は残す（TP1到達後ロジック用）
        assert lot2.tp1_hit is False                       # 他ロットは無傷
        assert lot2.take_profit_1 == pytest.approx(110.0)


def test_close_by_id_ignores_already_closed_lot():
    lot1_id, _, exit_order = _seed_two_lots("TSLA")
    close_trade_log_by_id(lot1_id, exit_order, 120.0, sold_qty=3)

    close_trade_log_by_id(lot1_id, exit_order, 130.0, sold_qty=3)  # 二重呼び出し

    with get_session() as session:
        lot1 = session.get(TradeLog, lot1_id)
        assert lot1.exit_price == pytest.approx(120.0)  # 上書きされない
        assert session.query(TradeLog).filter(TradeLog.ticker == "TSLA").count() == 2
