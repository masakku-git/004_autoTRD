"""SQLAlchemy DB接続の基盤設定（エンジン・セッション・テーブル初期化）"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import settings


# 全モデルが継承するベースクラス
class Base(DeclarativeBase):
    pass


# DB接続エンジンとセッションファクトリ
engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


def get_session() -> Session:
    """DBセッションを取得（with文で使用可能）"""
    return SessionLocal()


def init_db() -> None:
    """全モデルのテーブルをDBに作成（存在しない場合のみ）+ マイグレーション"""
    Base.metadata.create_all(engine)
    _migrate_trade_log(engine)


def _migrate_trade_log(eng) -> None:
    """TradeLogテーブルに不足カラムを追加（SQLite ALTER TABLE）"""
    from sqlalchemy import inspect, text

    inspector = inspect(eng)
    if "trade_log" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("trade_log")}
    migrations = [
        ("stop_loss", "FLOAT"),
        ("take_profit", "FLOAT"),
        ("take_profit_1", "FLOAT"),
        ("max_hold_days", "INTEGER DEFAULT 20"),
    ]
    with eng.begin() as conn:
        for col_name, col_type in migrations:
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE trade_log ADD COLUMN {col_name} {col_type}"))

