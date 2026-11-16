"""Add the optional default page size."""

from alembic import op
import sqlalchemy as sa

revision = "pagesizepref"
down_revision = "uilang"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('page_size', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('user_settings', 'page_size')
