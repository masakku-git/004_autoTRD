"""add commission to orders

約定時の手数料（moomoo order_fee_query の fee_amount = Commission + 消費税等の合計、USD）を
注文単位で記録する。NULL は「未取得」、0.0 は「取得済みで手数料なし」を表す
（区別できるよう NOT NULL にしない）。過去分は reconcile_fills.py が
commission が NULL の FILLED 注文を自動で埋める（APIは2018年以降の注文を遡れる）。

trade_log.pnl は従来どおりグロス（手数料控除前）のまま。

Revision ID: c7d2e5f1a9b3
Revises: a3f1c9d24b70
Create Date: 2026-09-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c7d2e5f1a9b3"
down_revision: Union[str, None] = "a3f1c9d24b70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("commission", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "commission")
