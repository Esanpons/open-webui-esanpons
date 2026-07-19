"""add collab usage telemetry tables (W15 Capa 1)

Revision ID: d5e6f7a8b9c0
Revises: 42e2978c7933
Create Date: 2026-07-17

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = '42e2978c7933'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    if 'collab_usage' not in tables:
        op.create_table(
            'collab_usage',
            sa.Column('id', sa.Text(), primary_key=True),
            sa.Column('channel_id', sa.Text(), nullable=False),
            sa.Column('agent_id', sa.Text(), nullable=False),
            sa.Column('call_type', sa.Text(), nullable=False),
            sa.Column('input_tokens', sa.Integer(), nullable=True),
            sa.Column('output_tokens', sa.Integer(), nullable=True),
            sa.Column('total_tokens', sa.Integer(), nullable=True),
            sa.Column('estimated_cost', sa.Float(), nullable=True),
            sa.Column('status', sa.Text(), nullable=False, server_default='success'),
            sa.Column('error_detail', sa.Text(), nullable=True),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
        )
        op.create_index(
            'idx_collab_usage_channel', 'collab_usage', ['channel_id', 'created_at']
        )
        op.create_index(
            'idx_collab_usage_agent', 'collab_usage', ['agent_id', 'created_at']
        )

    if 'collab_budget_tracker' not in tables:
        op.create_table(
            'collab_budget_tracker',
            sa.Column('channel_id', sa.Text(), primary_key=True),
            sa.Column('agent_id', sa.Text(), primary_key=True),
            sa.Column('consumed_tokens', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('consumed_cost', sa.Float(), nullable=False, server_default='0'),
            sa.Column('call_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('updated_at', sa.BigInteger(), nullable=False),
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    if 'collab_budget_tracker' in tables:
        op.drop_table('collab_budget_tracker')
    if 'collab_usage' in tables:
        op.drop_index('idx_collab_usage_agent', table_name='collab_usage')
        op.drop_index('idx_collab_usage_channel', table_name='collab_usage')
        op.drop_table('collab_usage')
