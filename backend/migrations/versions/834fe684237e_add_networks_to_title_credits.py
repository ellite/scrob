"""add networks to title_credits

Revision ID: 834fe684237e
Revises: 9f3e51c7ab24
Create Date: 2026-08-19 16:25:45.380709

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '834fe684237e'
down_revision: Union[str, Sequence[str], None] = '9f3e51c7ab24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'title_credits',
        sa.Column('networks', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('title_credits', 'networks')
