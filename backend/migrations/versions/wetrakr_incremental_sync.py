"""wetrakr incremental sync bookmarks

Revision ID: wetrakrinc
Revises: simklv2auth455
Create Date: 2026-10-09

wetrakr_last_push_at: the push only sends watch events/ratings newer than this
(NULL = never pushed, so one full push). wetrakr_last_activity: the
/sync/last_activities "all" stamp seen by the last pull, so an unchanged
account is skipped.
"""
from alembic import op
import sqlalchemy as sa


revision = "wetrakrinc"
down_revision = "simklv2auth455"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("wetrakr_last_push_at", sa.DateTime(), nullable=True))
    op.add_column("watch_events", sa.Column("origin", sa.String(length=16), nullable=True))
    op.add_column("user_settings", sa.Column("wetrakr_last_activity", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("watch_events", "origin")
    op.drop_column("user_settings", "wetrakr_last_activity")
    op.drop_column("user_settings", "wetrakr_last_push_at")
