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
    """全モデルのテーブルをDBに作成（存在しない場合のみ）"""
    Base.metadata.create_all(engine)
