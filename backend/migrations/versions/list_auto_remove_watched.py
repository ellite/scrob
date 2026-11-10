"""Add opt-in removal of newly watched list items."""

from alembic import op
import sqlalchemy as sa

revision = "listautowatch"
down_revision = "uilang"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('lists', sa.Column('auto_remove_watched', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('lists', sa.Column('auto_remove_since', sa.DateTime(), nullable=True))
    op.add_column('lists', sa.Column('auto_remove_include_earlier', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('lists', 'auto_remove_include_earlier')
    op.drop_column('lists', 'auto_remove_since')
    op.drop_column('lists', 'auto_remove_watched')
