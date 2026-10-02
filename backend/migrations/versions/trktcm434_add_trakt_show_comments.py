"""add trakt_show_comments setting

Revision ID: trktcm434
Revises: wetrakr429
Create Date: 2026-09-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'trktcm434'
down_revision: Union[str, Sequence[str], None] = 'wetrakr429'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('trakt_show_comments', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('user_settings', 'trakt_show_comments')
