"""Make password_hash nullable for OIDC accounts

Revision ID: 0002_nullable_password_hash
Revises: 0001_initial_schema
Create Date: 2026-04-16
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0002_nullable_password_hash'
down_revision: Union[str, Sequence[str], None] = '0001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('users', 'password_hash', nullable=True)


def downgrade() -> None:
    op.alter_column('users', 'password_hash', nullable=False)
