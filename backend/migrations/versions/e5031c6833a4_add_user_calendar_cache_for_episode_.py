"""add user_calendar_cache for episode calendar

Revision ID: e5031c6833a4
Revises: 834fe684237e
Create Date: 2026-08-19 19:42:22.766206

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'e5031c6833a4'
down_revision: Union[str, Sequence[str], None] = '834fe684237e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_calendar_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_calendar_cache_user"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("user_calendar_cache")
