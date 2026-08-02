"""add show rewatches and rewatch progress

Revision ID: d4adcaccbc4f
Revises: 8c3d0e5f7a2b
Create Date: 2026-08-02
"""

from alembic import op
import sqlalchemy as sa


revision = "d4adcaccbc4f"
down_revision = "8c3d0e5f7a2b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "show_rewatches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("show_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["show_id"], ["shows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "show_id", name="uq_show_rewatches_user_show"),
    )
    op.create_index(
        op.f("ix_show_rewatches_user_id"), "show_rewatches", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_show_rewatches_show_id"), "show_rewatches", ["show_id"], unique=False
    )

    op.create_table(
        "rewatch_progress",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rewatch_id", sa.Integer(), nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=False),
        sa.Column("watch_event_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["rewatch_id"], ["show_rewatches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_id"], ["media.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["watch_event_id"], ["watch_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rewatch_id", "media_id", name="uq_rewatch_progress_rewatch_media"),
    )
    op.create_index(
        op.f("ix_rewatch_progress_rewatch_id"), "rewatch_progress", ["rewatch_id"], unique=False
    )
    op.create_index(
        op.f("ix_rewatch_progress_media_id"), "rewatch_progress", ["media_id"], unique=False
    )
    op.create_index(
        op.f("ix_rewatch_progress_watch_event_id"), "rewatch_progress", ["watch_event_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_rewatch_progress_watch_event_id"), table_name="rewatch_progress")
    op.drop_index(op.f("ix_rewatch_progress_media_id"), table_name="rewatch_progress")
    op.drop_index(op.f("ix_rewatch_progress_rewatch_id"), table_name="rewatch_progress")
    op.drop_table("rewatch_progress")
    op.drop_index(op.f("ix_show_rewatches_show_id"), table_name="show_rewatches")
    op.drop_index(op.f("ix_show_rewatches_user_id"), table_name="show_rewatches")
    op.drop_table("show_rewatches")
