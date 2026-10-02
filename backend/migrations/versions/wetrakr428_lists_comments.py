"""add wetrakr lists + comments sync

Revision ID: wetrakr428
Revises: wetrakr426
Create Date: 2026-09-27

Adds list/comment sync+push flags to user_settings, plus a remote-id column
on lists and comments each to link a local row to its WeTrakr counterpart —
used both to dedup an import and (for comments, which have no update/dedup
endpoint on WeTrakr's side) to stop a repeated push from re-posting.
"""

from alembic import op
import sqlalchemy as sa

revision = "wetrakr428"
down_revision = "wetrakr426"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("wetrakr_sync_lists", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("user_settings", sa.Column("wetrakr_push_lists", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_settings", sa.Column("wetrakr_sync_comments", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("user_settings", sa.Column("wetrakr_push_comments", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("lists", sa.Column("wetrakr_list_id", sa.Integer(), nullable=True))
    op.add_column("comments", sa.Column("wetrakr_comment_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("comments", "wetrakr_comment_id")
    op.drop_column("lists", "wetrakr_list_id")
    op.drop_column("user_settings", "wetrakr_push_comments")
    op.drop_column("user_settings", "wetrakr_sync_comments")
    op.drop_column("user_settings", "wetrakr_push_lists")
    op.drop_column("user_settings", "wetrakr_sync_lists")
