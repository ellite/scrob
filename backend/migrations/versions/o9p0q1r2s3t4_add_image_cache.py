"""Add image cache settings and table

Revision ID: o9p0q1r2s3t4
Revises: n8o9p0q1r2s3
Create Date: 2026-06-03
"""
from alembic import op
import sqlalchemy as sa

revision = 'o9p0q1r2s3t4'
down_revision = 'n8o9p0q1r2s3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add fields to global_settings
    op.add_column('global_settings', sa.Column('image_cache_enabled', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('global_settings', sa.Column('image_cache_limit_gb', sa.Integer(), nullable=True))

    # 2. Create image_cache table
    op.create_table(
        'image_cache',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('path', sa.String(length=255), nullable=False),
        sa.Column('size', sa.String(length=50), nullable=False),
        sa.Column('image_type', sa.String(length=50), nullable=False, server_default='ondemand'),
        sa.Column('file_size', sa.Integer(), nullable=False),
        sa.Column('last_accessed', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    )

    # 3. Create indexes
    op.create_index('uq_image_cache_path_size', 'image_cache', ['path', 'size'], unique=True)
    op.create_index('idx_image_cache_type_accessed', 'image_cache', ['image_type', 'last_accessed'])


def downgrade() -> None:
    op.drop_index('idx_image_cache_type_accessed', table_name='image_cache')
    op.drop_index('uq_image_cache_path_size', table_name='image_cache')
    op.drop_table('image_cache')
    op.drop_column('global_settings', 'image_cache_limit_gb')
    op.drop_column('global_settings', 'image_cache_enabled')
