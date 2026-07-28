"""Allow watch events without a known date

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-07-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "watch_events",
        "watched_at",
        existing_type=sa.DateTime(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute("UPDATE watch_events SET watched_at = NOW() WHERE watched_at IS NULL")
    op.alter_column(
        "watch_events",
        "watched_at",
        existing_type=sa.DateTime(),
        nullable=False,
    )
