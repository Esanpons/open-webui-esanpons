"""Add collab_profile and collab_channel_config tables (W11/W12).

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-07-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collab_profile",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("agent_overrides", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("budget", sa.Text(), nullable=True),
        sa.Column("is_template", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "idx_collab_profile_user", "collab_profile", ["user_id"]
    )

    op.create_table(
        "collab_channel_config",
        sa.Column("channel_id", sa.Text(), primary_key=True),
        sa.Column("source_profile_id", sa.Text(), nullable=True),
        sa.Column("source_profile_version", sa.BigInteger(), nullable=True),
        sa.Column("config", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("agent_overrides", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("budget", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("collab_channel_config")
    op.drop_index("idx_collab_profile_user", table_name="collab_profile")
    op.drop_table("collab_profile")
