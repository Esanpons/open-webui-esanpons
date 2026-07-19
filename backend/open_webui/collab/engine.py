"""Persistent primitives for the collaborative scheduler (W1/W9/W10).

This module deliberately contains no model calls or socket code.  It owns the
durable event sequence, worker lease and per-agent receipts so the orchestrator
can be restarted without losing coordination state.
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from open_webui.internal.db import Base, JSONField, get_async_db_context
from sqlalchemy import BigInteger, Column, Index, Integer, Text, UniqueConstraint, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


SESSION_STATUSES = ("active", "idle", "stopped")
EVENT_STATUSES = ("active", "superseded", "consumed")
RECEIPT_STATES = (
    "received",
    "incorporated",
    "evaluating",
    "will_intervene",
    "pass",
)
AGENT_STATES = (
    "idle",
    "listening",
    "evaluating",
    "will_intervene",
    "pass",
    "speaking",
    "down",
)


class CollabSession(Base):
    __tablename__ = "collab_session"

    channel_id = Column(Text, primary_key=True)
    status = Column(Text, nullable=False, default="idle")
    lease_owner = Column(Text, nullable=True)
    lease_expires_at = Column(BigInteger, nullable=True)
    last_event_seq = Column(Integer, nullable=False, default=0)
    updated_at = Column(BigInteger, nullable=False)


class CollabEvent(Base):
    __tablename__ = "collab_event"
    __table_args__ = (
        UniqueConstraint("channel_id", "seq", name="uq_collab_event_channel_seq"),
        UniqueConstraint(
            "channel_id", "type", "message_id", name="uq_collab_event_message_type"
        ),
    )

    id = Column(Text, primary_key=True)
    channel_id = Column(Text, nullable=False)
    seq = Column(Integer, nullable=False)
    type = Column(Text, nullable=False)
    agent_id = Column(Text, nullable=True)
    message_id = Column(Text, nullable=True)
    payload = Column(JSONField, nullable=True)
    status = Column(Text, nullable=False, default="active")
    created_at = Column(BigInteger, nullable=False)


class CollabReceipt(Base):
    __tablename__ = "collab_receipt"
    __table_args__ = (
        UniqueConstraint("channel_id", "event_seq", "agent_id", name="uq_collab_receipt_event_agent"),
    )

    id = Column(Text, primary_key=True)
    event_seq = Column(Integer, nullable=False)
    channel_id = Column(Text, nullable=False)
    agent_id = Column(Text, nullable=False)
    state = Column(Text, nullable=False, default="received")
    message_id = Column(Text, nullable=True)
    updated_at = Column(BigInteger, nullable=False)


class CollabState(Base):
    """Key-value store per canal per separar l'estat de channel.meta (W4-3).

    Claus previstes: "summary", "phase", "down_agents".
    Una fila per (channel_id, key) amb upsert atòmic.
    """

    __tablename__ = "collab_state"
    __table_args__ = (
        UniqueConstraint("channel_id", "key", name="uq_collab_state_channel_key"),
    )

    channel_id = Column(Text, nullable=False)
    key = Column(Text, nullable=False)
    value = Column(JSONField, nullable=True)
    updated_at = Column(BigInteger, nullable=False)
    id = Column(Text, primary_key=True)


class CollabTask(Base):
    """Fila independent del tauler compartit (W4-4)."""

    __tablename__ = "collab_task"
    __table_args__ = (
        Index("idx_collab_task_channel", "channel_id"),
        Index("idx_collab_task_status", "channel_id", "status"),
    )

    id = Column(Text, primary_key=True)
    channel_id = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="pending")
    assignee = Column(Text, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    created_by = Column(Text, nullable=False, default="")
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)


@asynccontextmanager
async def _session_scope(db=None):
    if db is not None:
        yield db, False
    else:
        async with get_async_db_context() as session:
            yield session, True


def _insert_for(session, model):
    return pg_insert(model) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(model)


async def _ensure_session(session, channel_id: str, now: int) -> None:
    statement = (
        _insert_for(session, CollabSession)
        .values(channel_id=channel_id, status="idle", last_event_seq=0, updated_at=now)
        .on_conflict_do_nothing(index_elements=["channel_id"])
    )
    await session.execute(statement)


async def append_event(
    channel_id: str,
    event_type: str,
    *,
    agent_id: str | None = None,
    message_id: str | None = None,
    payload: dict | None = None,
    status: str = "active",
    db=None,
) -> CollabEvent:
    """Append an event with a channel-monotonic sequence, atomically."""
    if status not in EVENT_STATUSES:
        raise ValueError(f"invalid event status: {status}")
    now = int(time.time())
    async with _session_scope(db) as (session, owns_session):
        await _ensure_session(session, channel_id, now)
        result = await session.execute(
            update(CollabSession)
            .where(CollabSession.channel_id == channel_id)
            .values(
                last_event_seq=CollabSession.last_event_seq + 1,
                updated_at=now,
            )
            .returning(CollabSession.last_event_seq)
        )
        seq = int(result.scalar_one())
        event = CollabEvent(
            id=str(uuid.uuid4()),
            channel_id=channel_id,
            seq=seq,
            type=event_type,
            agent_id=agent_id,
            message_id=message_id,
            payload=payload or {},
            status=status,
            created_at=now,
        )
        session.add(event)
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return event


async def create_receipts(
    channel_id: str,
    event_seq: int,
    agents: list[str],
    *,
    message_id: str | None = None,
    db=None,
) -> None:
    now = int(time.time())
    async with _session_scope(db) as (session, owns_session):
        for agent_id in dict.fromkeys(agents):
            await session.execute(
                _insert_for(session, CollabReceipt)
                .values(
                    id=str(uuid.uuid4()),
                    event_seq=event_seq,
                    channel_id=channel_id,
                    agent_id=agent_id,
                    state="received",
                    message_id=message_id,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["channel_id", "event_seq", "agent_id"]
                )
            )
        if owns_session:
            await session.commit()
        else:
            await session.flush()


async def record_user_message(
    channel_id: str,
    agents: list[str],
    *,
    message_id: str | None = None,
    payload: dict | None = None,
    db=None,
) -> CollabEvent:
    """Persisteix event, receipts i invalidació prèvia en una transacció."""
    async with _session_scope(db) as (session, owns_session):
        now = int(time.time())
        await _ensure_session(session, channel_id, now)
        # Serialitza els missatges del canal abans de comprovar message_id.
        # Això evita que dos workers creïn el mateix event i manté el seq
        # sense buits tant a SQLite com a PostgreSQL.
        await session.execute(
            update(CollabSession)
            .where(CollabSession.channel_id == channel_id)
            .values(updated_at=now)
        )
        existing = None
        if message_id is not None:
            result = await session.execute(
                select(CollabEvent).where(
                    CollabEvent.channel_id == channel_id,
                    CollabEvent.type == "user_message",
                    CollabEvent.message_id == message_id,
                )
            )
            existing = result.scalar_one_or_none()
        if existing is not None:
            await create_receipts(
                channel_id,
                existing.seq,
                agents,
                message_id=message_id,
                db=session,
            )
            if owns_session:
                await session.commit()
            else:
                await session.flush()
            return existing
        event = await append_event(
            channel_id,
            "user_message",
            message_id=message_id,
            payload=payload,
            db=session,
        )
        await create_receipts(
            channel_id,
            event.seq,
            agents,
            message_id=message_id,
            db=session,
        )
        await supersede_handraises(channel_id, event.seq, db=session)
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return event


async def update_receipt(
    channel_id: str, event_seq: int, agent_id: str, state: str, *, db=None
) -> bool:
    if state not in RECEIPT_STATES:
        raise ValueError(f"invalid receipt state: {state}")
    async with _session_scope(db) as (session, owns_session):
        result = await session.execute(
            update(CollabReceipt)
            .where(
                CollabReceipt.channel_id == channel_id,
                CollabReceipt.event_seq == event_seq,
                CollabReceipt.agent_id == agent_id,
            )
            .values(state=state, updated_at=int(time.time()))
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return result.rowcount > 0


async def receipt_summary(channel_id: str, event_seq: int, *, db=None) -> dict[str, int]:
    async with _session_scope(db) as (session, _owns_session):
        result = await session.execute(
            select(CollabReceipt.state).where(
                CollabReceipt.channel_id == channel_id,
                CollabReceipt.event_seq == event_seq,
            )
        )
        summary = {state: 0 for state in RECEIPT_STATES}
        for state in result.scalars().all():
            summary[state] = summary.get(state, 0) + 1
        summary["total"] = sum(summary.values())
        return summary


async def list_receipts(channel_id: str, event_seq: int, *, db=None):
    async with _session_scope(db) as (session, _owns_session):
        result = await session.execute(
            select(CollabReceipt)
            .where(
                CollabReceipt.channel_id == channel_id,
                CollabReceipt.event_seq == event_seq,
            )
            .order_by(CollabReceipt.agent_id.asc())
        )
        return list(result.scalars().all())


async def transition_receipt(
    channel_id: str,
    event_seq: int,
    agent_id: str,
    state: str,
    *,
    db=None,
):
    """Actualitza un receipt i registra l'agent_state en una transacció."""
    async with _session_scope(db) as (session, owns_session):
        changed = await update_receipt(
            channel_id, event_seq, agent_id, state, db=session
        )
        if not changed:
            if owns_session:
                await session.rollback()
            return None, await receipt_summary(channel_id, event_seq, db=session)
        summary = await receipt_summary(channel_id, event_seq, db=session)
        event = await append_event(
            channel_id,
            "agent_state",
            agent_id=agent_id,
            payload={
                "state": state,
                "receipt_event_seq": event_seq,
                "summary": summary,
            },
            db=session,
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return event, summary


async def supersede_handraises(channel_id: str, before_seq: int, *, db=None) -> int:
    async with _session_scope(db) as (session, owns_session):
        result = await session.execute(
            update(CollabEvent)
            .where(
                CollabEvent.channel_id == channel_id,
                CollabEvent.seq < before_seq,
                CollabEvent.type == "handraise",
                CollabEvent.status == "active",
            )
            .values(status="superseded")
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return result.rowcount


async def acquire_lease(channel_id: str, owner: str, *, ttl: int = 30, db=None) -> bool:
    now = int(time.time())
    async with _session_scope(db) as (session, owns_session):
        await _ensure_session(session, channel_id, now)
        result = await session.execute(
            update(CollabSession)
            .where(
                CollabSession.channel_id == channel_id,
                (CollabSession.lease_owner.is_(None))
                | (CollabSession.lease_owner == owner)
                | (CollabSession.lease_expires_at < now),
            )
            .values(
                status="active",
                lease_owner=owner,
                lease_expires_at=now + ttl,
                updated_at=now,
            )
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return result.rowcount > 0


async def renew_lease(channel_id: str, owner: str, *, ttl: int = 30, db=None) -> bool:
    """Renova només un lease vigent que encara pertany al mateix worker."""
    now = int(time.time())
    async with _session_scope(db) as (session, owns_session):
        result = await session.execute(
            update(CollabSession)
            .where(
                CollabSession.channel_id == channel_id,
                CollabSession.lease_owner == owner,
                CollabSession.lease_expires_at >= now,
            )
            .values(lease_expires_at=now + ttl, updated_at=now)
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return result.rowcount > 0


async def release_lease(channel_id: str, owner: str, *, stopped: bool = False, db=None) -> bool:
    async with _session_scope(db) as (session, owns_session):
        result = await session.execute(
            update(CollabSession)
            .where(
                CollabSession.channel_id == channel_id,
                CollabSession.lease_owner == owner,
            )
            .values(
                status="stopped" if stopped else "idle",
                lease_owner=None,
                lease_expires_at=None,
                updated_at=int(time.time()),
            )
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return result.rowcount > 0


async def list_events(channel_id: str, *, since: int = 0, limit: int = 200, db=None):
    limit = max(1, min(limit, 1000))
    async with _session_scope(db) as (session, _owns_session):
        result = await session.execute(
            select(CollabEvent)
            .where(CollabEvent.channel_id == channel_id, CollabEvent.seq > since)
            .order_by(CollabEvent.seq.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def reconcile_expired_session(channel_id: str, *, db=None) -> bool:
    """Allibera una sessió activa abandonada amb el lease expirat.

    Els events actius es conserven: són la cua persistent recuperable.
    """
    now = int(time.time())
    async with _session_scope(db) as (session, owns_session):
        result = await session.execute(
            update(CollabSession)
            .where(
                CollabSession.channel_id == channel_id,
                CollabSession.status == "active",
                CollabSession.lease_expires_at.is_not(None),
                CollabSession.lease_expires_at < now,
            )
            .values(
                status="idle",
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return result.rowcount > 0


# ---------------------------------------------------------------------------
# W4-3: Key-value store per canal (collab_state)
# Separa l'estat col·laboratiu de channel.meta per evitar races
# read-modify-write (S1) i estat barrejat (S8).
# ---------------------------------------------------------------------------


async def get_state_value(
    channel_id: str, key: str, default=None, *, db=None
):
    """Retrieu el valor d'una clau de collab_state. Retorna *default* si no existeix."""
    async with _session_scope(db) as (session, owns):
        result = await session.execute(
            select(CollabState.value).where(
                CollabState.channel_id == channel_id,
                CollabState.key == key,
            )
        )
        val = result.scalar_one_or_none()
        if val is not None:
            return val
        return default


async def set_state_value(
    channel_id: str, key: str, value, *, db=None
) -> None:
    """Desa (o sobreescriu) un valor a collab_state. Upsert atòmic per (channel_id, key)."""
    now = int(time.time())
    async with _session_scope(db) as (session, owns):
        insert_fn = _insert_for(session, CollabState)
        await session.execute(
            insert_fn
            .values(
                id=str(uuid.uuid4()),
                channel_id=channel_id,
                key=key,
                value=value,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["channel_id", "key"],
                set_={"value": value, "updated_at": now},
            )
        )
        if owns:
            await session.commit()
        else:
            await session.flush()
