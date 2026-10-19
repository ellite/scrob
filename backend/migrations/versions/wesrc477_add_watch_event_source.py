"""add watch_events.source (#477)

Revision ID: wesrc477
Revises: playbtn485
Create Date: 2026-10-18
"""

from alembic import op
import sqlalchemy as sa


revision = "wesrc477"
down_revision = "playbtn485"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watch_events", sa.Column("source", sa.String(16), nullable=True))
    # The only provenance recorded before this column; everything else stays
    # NULL (unknown) rather than being guessed.
    op.execute("UPDATE watch_events SET source = origin WHERE origin IS NOT NULL")


def downgrade() -> None:
    op.drop_column("watch_events", "source")
