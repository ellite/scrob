"""add user_settings.ui_language

Revision ID: uilang
Revises: condhist391
Create Date: 2026-09-15

Per-user web UI language (null = follow the browser's Accept-Language).
Separate from user_profiles.metadata_language, which only localizes
TMDB/TVDB metadata.
"""

from alembic import op
import sqlalchemy as sa

revision = "uilang"
down_revision = "condhist391"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("ui_language", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "ui_language")
