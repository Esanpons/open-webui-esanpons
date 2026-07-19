"""Estat compartit de l'espai col·laboratiu.

Les tasques viuen a ``collab_task`` i l'estat operatiu a ``collab_state``.
Només les propostes de consens continuen temporalment a ``channel.meta``.
"""

import logging
import secrets
import threading
import time

from open_webui.collab.engine import (
    CollabTask,
    _session_scope,
    get_state_value,
    set_state_value,
)
from open_webui.internal.db import get_async_db_context
from open_webui.models.channels import Channel
from sqlalchemy import delete, select, update

log = logging.getLogger(__name__)

TASK_STATUSES = ("pending", "doing", "done")
_task_timestamp_lock = threading.Lock()
_last_task_timestamp = 0


def _next_task_timestamp() -> int:
    """Retorna un timestamp estrictament creixent dins del worker actual."""
    global _last_task_timestamp
    with _task_timestamp_lock:
        now = time.time_ns()
        _last_task_timestamp = max(now, _last_task_timestamp + 1)
        return _last_task_timestamp


async def get_meta_key(channel_id: str, key: str, default=None):
    async with get_async_db_context() as db:
        result = await db.execute(select(Channel).filter(Channel.id == channel_id))
        channel = result.scalars().first()
        if not channel:
            return default
        return (channel.meta or {}).get(key, default)


async def set_meta_key(channel_id: str, key: str, value) -> bool:
    """Escriu una clau de channel.meta preservant la resta (config inclosa).
    value=None elimina la clau.

    Compare-and-swap sobre meta_version amb reintents: sense això, un
    read-modify-write concurrent amb el desat de config del panell (que també
    reescriu meta sencer) podria perdre silenciosament l'altra escriptura.
    """
    for _attempt in range(4):
        async with get_async_db_context() as db:
            result = await db.execute(select(Channel).filter(Channel.id == channel_id))
            channel = result.scalars().first()
            if not channel:
                return False
            current_version = channel.meta_version or 0
            meta = {**(channel.meta or {})}
            if value is None:
                if key not in meta:
                    return True  # res a fer, evita un bump de versió inútil
                meta.pop(key, None)
            else:
                meta[key] = value
            update_result = await db.execute(
                update(Channel)
                .where(
                    Channel.id == channel_id,
                    Channel.meta_version == current_version,
                )
                .values(meta=meta, meta_version=current_version + 1)
            )
            if update_result.rowcount == 0:
                await db.rollback()
                continue  # algú altre ha escrit meta mentrestant: rellegeix
            await db.commit()
            return True
    log.warning("set_meta_key(%s, %s): abandonat per contenció", channel_id, key)
    return False


############################
# Tauler de tasques
############################


def _task_dict(task: CollabTask) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "assignee": task.assignee,
        "notes": task.notes,
        "created_by": task.created_by,
    }


async def get_tasks(channel_id: str, *, db=None) -> list[dict]:
    async with _session_scope(db) as (session, _owns_session):
        result = await session.execute(
            select(CollabTask)
            .where(CollabTask.channel_id == channel_id)
            .order_by(CollabTask.created_at.asc(), CollabTask.id.asc())
        )
        return [_task_dict(task) for task in result.scalars().all()]


async def create_task(
    channel_id: str,
    title: str,
    created_by: str = "",
    assignee: str = "",
    *,
    db=None,
) -> dict:
    now = _next_task_timestamp()
    # 10 hex: prou curt per escriure'l al xat i prou llarg perquè una col·lisió
    # de la clau primària (global, no per canal) sigui negligible.
    task = CollabTask(
        id=secrets.token_hex(5),
        channel_id=channel_id,
        title=title.strip(),
        status="pending",
        assignee=assignee.strip(),
        notes="",
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    async with _session_scope(db) as (session, owns_session):
        session.add(task)
        if owns_session:
            await session.commit()
        else:
            await session.flush()
    return _task_dict(task)


async def update_task(
    channel_id: str,
    task_id: str,
    title: str = "",
    status: str = "",
    assignee: str = "",
    notes: str = "",
    *,
    db=None,
) -> tuple[bool, str]:
    """Actualitza els camps no buits d'una tasca. Retorna (ok, motiu)."""
    if status and status not in TASK_STATUSES:
        return False, f"Estat invàlid: {status} (vàlids: {', '.join(TASK_STATUSES)})"
    values = {"updated_at": time.time_ns()}
    if title:
        values["title"] = title.strip()
    if status:
        values["status"] = status
    if assignee:
        values["assignee"] = assignee.strip()
    if notes:
        values["notes"] = notes.strip()
    async with _session_scope(db) as (session, owns_session):
        result = await session.execute(
            update(CollabTask)
            .where(CollabTask.channel_id == channel_id, CollabTask.id == task_id)
            .values(**values)
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        if result.rowcount:
            return True, "Tasca actualitzada."
    return False, f"No existeix cap tasca amb id {task_id}"


async def replace_tasks(channel_id: str, items: list[dict], *, db=None) -> list[dict]:
    """Substitueix TOT el tauler del canal per la llista donada (plantilles).

    Cada item: {title, status?, assignee?, notes?, created_by?}. Els estats
    invàlids cauen a "pending". Retorna el tauler resultant.
    """
    async with _session_scope(db) as (session, owns_session):
        await session.execute(delete(CollabTask).where(CollabTask.channel_id == channel_id))
        for item in items or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            status = item.get("status", "pending")
            now = _next_task_timestamp()
            session.add(
                CollabTask(
                    id=secrets.token_hex(5),
                    channel_id=channel_id,
                    title=title,
                    status=status if status in TASK_STATUSES else "pending",
                    assignee=str(item.get("assignee") or "").strip(),
                    notes=str(item.get("notes") or "").strip(),
                    created_by=str(item.get("created_by") or ""),
                    created_at=now,
                    updated_at=now,
                )
            )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
    return await get_tasks(channel_id, db=db)


async def delete_task(channel_id: str, task_id: str, *, db=None) -> bool:
    async with _session_scope(db) as (session, owns_session):
        result = await session.execute(
            delete(CollabTask).where(
                CollabTask.channel_id == channel_id, CollabTask.id == task_id
            )
        )
        if owns_session:
            await session.commit()
        else:
            await session.flush()
        return result.rowcount > 0


def tasks_as_text(tasks: list[dict]) -> str:
    if not tasks:
        return "(cap tasca al tauler)"
    icons = {"pending": "⬜", "doing": "🔵", "done": "✅"}
    lines = []
    for t in tasks:
        assignee = f" → {t['assignee']}" if t.get("assignee") else ""
        notes = f" ({t['notes']})" if t.get("notes") else ""
        lines.append(f"{icons.get(t.get('status'), '⬜')} [{t.get('id')}] {t.get('title')}{assignee}{notes}")
    return "\n".join(lines)


############################
# Resum incremental
############################


async def get_summary(channel_id: str, *, db=None) -> str:
    return await get_state_value(channel_id, "summary", "", db=db) or ""


async def set_summary(channel_id: str, summary: str, *, db=None) -> bool:
    await set_state_value(channel_id, "summary", summary.strip() or None, db=db)
    return True


############################
# Fase de treball de l'equip (filosofia: primer planificar, després executar)
############################

PHASES = ("planning", "execution")


async def get_phase(channel_id: str, *, db=None) -> str:
    phase = await get_state_value(channel_id, "phase", "planning", db=db)
    return phase if phase in PHASES else "planning"


async def set_phase(channel_id: str, phase: str, *, db=None) -> bool:
    await set_state_value(
        channel_id, "phase", phase if phase in PHASES else "planning", db=db
    )
    return True


############################
# Agents caiguts (sense tokens, timeouts, errors) — visible per a tot l'equip
############################


async def get_down_agents(channel_id: str, *, db=None) -> dict:
    down = await get_state_value(channel_id, "down_agents", {}, db=db)
    return down if isinstance(down, dict) else {}


async def set_down_agent(
    channel_id: str, agent_id: str, reason: str, *, db=None
) -> bool:
    down = await get_down_agents(channel_id, db=db)
    down[agent_id] = {"reason": reason, "since": int(time.time())}
    await set_state_value(channel_id, "down_agents", down, db=db)
    return True


async def clear_down_agent(channel_id: str, agent_id: str, *, db=None) -> bool:
    down = await get_down_agents(channel_id, db=db)
    if agent_id not in down:
        return False
    down.pop(agent_id, None)
    await set_state_value(channel_id, "down_agents", down or None, db=db)
    return True


############################
# Propostes de consens (pla acordat / feina acabada)
############################


async def get_end_proposal(channel_id: str):
    return await get_meta_key(channel_id, "collab_end_proposal", None)


async def set_end_proposal(channel_id: str, by: str, summary: str, kind: str = "finish") -> bool:
    """kind: 'finish' (donar la feina per acabada) o 'plan' (donar el pla per acordat)."""
    return await set_meta_key(
        channel_id, "collab_end_proposal", {"by": by, "summary": summary.strip(), "kind": kind}
    )


async def clear_end_proposal(channel_id: str) -> bool:
    return await set_meta_key(channel_id, "collab_end_proposal", None)
