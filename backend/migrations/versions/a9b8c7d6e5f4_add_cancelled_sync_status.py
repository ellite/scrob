"""add cancelled sync status

Revision ID: a9b8c7d6e5f4
Revises: 175fb7fcc9a1
Create Date: 2026-07-27
"""

from alembic import op


revision = "a9b8c7d6e5f4"
down_revision = "175fb7fcc9a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE syncstatus ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely in-place. Keep the value;
    # older application versions simply do not set it.
    pass
