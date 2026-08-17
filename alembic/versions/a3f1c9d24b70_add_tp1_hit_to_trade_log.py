"""add tp1_hit to trade_log

段階利確(TP1)の消化を take_profit_1 の NULL 化で表現していたため、
「TP1到達後のみ有効」な戦略ロジック（breakout_v6 の RSI決済は
take_profit_1 > 0 かつ highest_price >= take_profit_1 で判定する）が
TP1消化後に永久に発火しなくなっていた。消化の記録を専用フラグに移し、
take_profit_1 の値は保持し続けるようにする。

既存行は tp1_hit=false で開始する。既にTP1を消化した行は take_profit_1 が
NULL 化済みで元の値を復元できないため、それらのロットのRSI決済は
引き続き発火しない（新規ロットから正しく動作する）。

Revision ID: a3f1c9d24b70
Revises: bcb1ffe8f8ec
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a3f1c9d24b70"
down_revision: Union[str, None] = "bcb1ffe8f8ec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trade_log",
        sa.Column(
            "tp1_hit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("trade_log", "tp1_hit")
