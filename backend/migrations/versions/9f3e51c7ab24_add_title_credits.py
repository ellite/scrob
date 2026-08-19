"""add title_credits for people/studio stats

Revision ID: 9f3e51c7ab24
Revises: abbf61c1c534
Create Date: 2026-08-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "9f3e51c7ab24"
down_revision = "abbf61c1c534"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "title_credits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("cast", JSONB(), nullable=False),
        sa.Column("directors", JSONB(), nullable=False),
        sa.Column("writers", JSONB(), nullable=False),
        sa.Column("studios", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tmdb_id", "media_type", name="uq_title_credits_title"),
    )
    op.create_index(
        op.f("ix_title_credits_tmdb_id"),
        "title_credits",
        ["tmdb_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_title_credits_tmdb_id"), table_name="title_credits")
    op.drop_table("title_credits")
