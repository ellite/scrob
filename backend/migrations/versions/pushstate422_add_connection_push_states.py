"""add connection_push_states table

Revision ID: pushstate422
Revises: condhist391
Create Date: 2026-09-21

Remembers what a scheduled push last sent to each connection so the next one
can skip unchanged items instead of re-sending the whole snapshot (#421, #422).
"""

from alembic import op
import sqlalchemy as sa

revision = "pushstate422"
down_revision = "condhist391"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connection_push_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "connection_id", sa.Integer(),
            sa.ForeignKey("media_server_connections.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("media_id", sa.Integer(), sa.ForeignKey("media.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_key", sa.String(40), nullable=False),
        sa.Column("value", sa.Float(precision=53), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        # Also serves the per-connection load, which filters on connection_id first.
        sa.UniqueConstraint("connection_id", "media_id", "item_key", name="uq_connection_push_state_item"),
    )


def downgrade() -> None:
    op.drop_table("connection_push_states")
