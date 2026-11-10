"""Add an optional saved order for personal lists."""

from alembic import op
import sqlalchemy as sa

revision = "listorder"
down_revision = "uilang"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('lists', sa.Column('sort_order', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('lists', 'sort_order')
