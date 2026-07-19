"""add persistent collab scheduler tables (W1/W9/W10)

Revision ID: f6a7b8c9d0e1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from open_webui.internal.db import JSONField


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collab_session",
        sa.Column("channel_id", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("last_event_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "collab_event",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("payload", JSONField(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("channel_id", "seq", name="uq_collab_event_channel_seq"),
        sa.UniqueConstraint(
            "channel_id", "type", "message_id", name="uq_collab_event_message_type"
        ),
    )
    op.create_index("idx_collab_event_channel_status", "collab_event", ["channel_id", "status", "seq"])
    op.create_table(
        "collab_receipt",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="received"),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "channel_id", "event_seq", "agent_id", name="uq_collab_receipt_event_agent"
        ),
    )
    op.create_index(
        "idx_collab_receipt_channel_event", "collab_receipt", ["channel_id", "event_seq"]
    )


def downgrade() -> None:
    op.drop_index("idx_collab_receipt_channel_event", table_name="collab_receipt")
    op.drop_table("collab_receipt")
    op.drop_index("idx_collab_event_channel_status", table_name="collab_event")
    op.drop_table("collab_event")
    op.drop_table("collab_session")
