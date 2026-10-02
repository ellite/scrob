"""widen wetrakr remote-id columns to bigint

Revision ID: wetrakr429
Revises: wetrakr428
Create Date: 2026-09-27

lists.wetrakr_list_id and comments.wetrakr_comment_id were added as INTEGER,
but WeTrakr comment ids observed in practice (e.g. 3000053946) exceed
Postgres INTEGER's 32-bit range, crashing the comment pull with a
DataError. Widen both to BIGINT.
"""

from alembic import op
import sqlalchemy as sa

revision = "wetrakr429"
down_revision = "wetrakr428"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("lists", "wetrakr_list_id", type_=sa.BigInteger())
    op.alter_column("comments", "wetrakr_comment_id", type_=sa.BigInteger())


def downgrade() -> None:
    op.alter_column("comments", "wetrakr_comment_id", type_=sa.Integer())
    op.alter_column("lists", "wetrakr_list_id", type_=sa.Integer())
