"""add auto sync/push intervals for Trakt, Simkl, and MDBList

Revision ID: 5c1ef72c8859
Revises: d7e8f9a0b1c2
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa


revision = "5c1ef72c8859"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


COLUMNS = [
    "trakt_auto_sync_interval",
    "trakt_auto_push_interval",
    "simkl_auto_sync_interval",
    "simkl_auto_push_interval",
    "mdblist_auto_sync_interval",
    "mdblist_auto_push_interval",
]


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column(
            "user_settings",
            sa.Column(column, sa.Float(), nullable=True),
        )


def downgrade() -> None:
    for column in reversed(COLUMNS):
        op.drop_column("user_settings", column)
