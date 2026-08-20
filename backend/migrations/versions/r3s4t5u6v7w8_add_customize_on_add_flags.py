"""add radarr/sonarr customize_on_add flags to user_settings and global_settings

Revision ID: r3s4t5u6v7w8
Revises: e5031c6833a4
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'r3s4t5u6v7w8'
down_revision = 'e5031c6833a4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('user_settings', sa.Column('radarr_customize_on_add', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('user_settings', sa.Column('sonarr_customize_on_add', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('global_settings', sa.Column('radarr_customize_on_add', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('global_settings', sa.Column('sonarr_customize_on_add', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    op.drop_column('global_settings', 'sonarr_customize_on_add')
    op.drop_column('global_settings', 'radarr_customize_on_add')
    op.drop_column('user_settings', 'sonarr_customize_on_add')
    op.drop_column('user_settings', 'radarr_customize_on_add')
