"""drop allow_public_profiles global setting

Superseded by enable_logged_out_navigation, which now covers everything it
did (anonymous profile/list viewing) plus general browsing.

Revision ID: abbf61c1c534
Revises: 7786946fb622
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa


revision = "abbf61c1c534"
down_revision = "7786946fb622"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("global_settings", "allow_public_profiles")


def downgrade() -> None:
    op.add_column(
        "global_settings",
        sa.Column("allow_public_profiles", sa.Boolean(), nullable=False, server_default="false"),
    )
