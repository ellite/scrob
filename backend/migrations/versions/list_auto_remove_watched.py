"""Add opt-in removal of newly watched list items, with a restore trash."""

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
    op.create_table(
        'list_item_removals',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('list_id', sa.Integer(), sa.ForeignKey('lists.id', ondelete='CASCADE'), nullable=False),
        sa.Column('media_id', sa.Integer(), sa.ForeignKey('media.id', ondelete='CASCADE'), nullable=False),
        sa.Column('season_number', sa.Integer(), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('removed_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_list_item_removals_user_removed', 'list_item_removals', ['user_id', 'removed_at'])


def downgrade() -> None:
    op.drop_index('ix_list_item_removals_user_removed', table_name='list_item_removals')
    op.drop_table('list_item_removals')
    op.drop_column('lists', 'auto_remove_include_earlier')
    op.drop_column('lists', 'auto_remove_since')
    op.drop_column('lists', 'auto_remove_watched')
