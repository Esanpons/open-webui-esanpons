"""Add persistent collaborative tasks and migrate legacy state.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-18
"""

from typing import Sequence, Union
import json
import time
import uuid

from alembic import op
import sqlalchemy as sa

from open_webui.internal.db import JSONField


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _migrate_from_channel_meta() -> None:
    bind = op.get_bind()
    channel = sa.table(
        "channel",
        sa.column("id", sa.Text()),
        sa.column("meta", JSONField()),
    )
    state = sa.table(
        "collab_state",
        sa.column("id", sa.Text()),
        sa.column("channel_id", sa.Text()),
        sa.column("key", sa.Text()),
        sa.column("value", JSONField()),
        sa.column("updated_at", sa.BigInteger()),
    )
    task = sa.table(
        "collab_task",
        sa.column("id", sa.Text()),
        sa.column("channel_id", sa.Text()),
        sa.column("title", sa.Text()),
        sa.column("status", sa.Text()),
        sa.column("assignee", sa.Text()),
        sa.column("notes", sa.Text()),
        sa.column("created_by", sa.Text()),
        sa.column("created_at", sa.BigInteger()),
        sa.column("updated_at", sa.BigInteger()),
    )
    now = int(time.time())
    used_task_ids: set[str] = set()
    existing_state = set(
        bind.execute(sa.select(state.c.channel_id, state.c.key)).all()
    )
    valid_statuses = {"pending", "doing", "done"}

    for row in bind.execute(sa.select(channel.c.id, channel.c.meta)):
        meta = _as_dict(row.meta)
        for legacy_key, state_key in (
            ("collab_summary", "summary"),
            ("collab_phase", "phase"),
            ("collab_down_agents", "down_agents"),
        ):
            state_identity = (row.id, state_key)
            if (
                legacy_key in meta
                and meta[legacy_key] is not None
                and state_identity not in existing_state
            ):
                bind.execute(
                    state.insert().values(
                        id=str(uuid.uuid4()),
                        channel_id=row.id,
                        key=state_key,
                        value=meta[legacy_key],
                        updated_at=now,
                    )
                )
                existing_state.add(state_identity)

        legacy_tasks = meta.get("collab_tasks", [])
        if not isinstance(legacy_tasks, list):
            continue
        for position, legacy in enumerate(legacy_tasks):
            if not isinstance(legacy, dict):
                continue
            task_id = str(legacy.get("id") or uuid.uuid4())
            if task_id in used_task_ids:
                task_id = str(uuid.uuid4())
            used_task_ids.add(task_id)
            status = legacy.get("status", "pending")
            bind.execute(
                task.insert().values(
                    id=task_id,
                    channel_id=row.id,
                    title=str(legacy.get("title") or "").strip(),
                    status=status if status in valid_statuses else "pending",
                    assignee=str(legacy.get("assignee") or "").strip(),
                    notes=str(legacy.get("notes") or "").strip(),
                    created_by=str(legacy.get("created_by") or ""),
                    created_at=now * 1_000_000_000 + position,
                    updated_at=now * 1_000_000_000 + position,
                )
            )


def upgrade() -> None:
    op.create_table(
        "collab_task",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("assignee", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("idx_collab_task_channel", "collab_task", ["channel_id"])
    op.create_index(
        "idx_collab_task_status", "collab_task", ["channel_id", "status"]
    )
    _migrate_from_channel_meta()


def downgrade() -> None:
    op.drop_index("idx_collab_task_status", table_name="collab_task")
    op.drop_index("idx_collab_task_channel", table_name="collab_task")
    op.drop_table("collab_task")
