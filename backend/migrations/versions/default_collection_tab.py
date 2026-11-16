"""Add the default collection tab preference."""

from alembic import op
import sqlalchemy as sa

revision = "defaultcoltab"
down_revision = "uilang"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('default_collection_tab', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('user_settings', 'default_collection_tab')
