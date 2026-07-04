"""add highest_price to trade_log

Revision ID: bcb1ffe8f8ec
Revises:
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "bcb1ffe8f8ec"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trade_log", sa.Column("highest_price", sa.Float(), nullable=True))
    op.execute("UPDATE trade_log SET highest_price = entry_price WHERE highest_price IS NULL")


def downgrade() -> None:
    op.drop_column("trade_log", "highest_price")
