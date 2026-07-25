"""merge episode order and nuvio push heads

Revision ID: 175fb7fcc9a1
Revises: y9z0a1b2c3d4, z0a1b2c3d4e5
Create Date: 2026-07-25 19:26:12.270388

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '175fb7fcc9a1'
down_revision: Union[str, Sequence[str], None] = ('y9z0a1b2c3d4', 'z0a1b2c3d4e5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
