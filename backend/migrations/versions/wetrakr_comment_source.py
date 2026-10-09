"""wetrakr comment source

Revision ID: wetrakrsrc
Revises: wetrakrinc
Create Date: 2026-10-09

comments.wetrakr_source: the `source` WeTrakr reports for a comment, needed to
credit its platform ("TV Time review from WeTrakr") next to every WeTrakr comment.
"""
from alembic import op
import sqlalchemy as sa


revision = "wetrakrsrc"
down_revision = "wetrakrinc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("comments", sa.Column("wetrakr_source", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("comments", "wetrakr_source")
