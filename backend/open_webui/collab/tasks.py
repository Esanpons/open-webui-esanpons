"""Estat compartit de l'espai col·laboratiu: tauler de tasques, resum
incremental i proposta de tancament. Tot desat a channel.meta (claus pròpies,
separades de la config perquè eines i orquestrador hi escriguin sense trepitjar
la configuració de l'usuari).

Claus a channel.meta:
- collab_tasks:        [{id, title, status, assignee, notes, created_by}]
- collab_summary:      str — resum incremental de la feina (Fase 4)
- collab_end_proposal: {by, summary} — proposta "donem-ho per acabat" pendent de vot
"""

import logging
import secrets
import time

from open_webui.internal.db import get_async_db_context
from open_webui.models.channels import Channel
from sqlalchemy import select

log = logging.getLogger(__name__)

TASK_STATUSES = ("pending", "doing", "done")


async def get_meta_key(channel_id: str, key: str, default=None):
    async with get_async_db_context() as db:
        result = await db.execute(select(Channel).filter(Channel.id == channel_id))
        channel = result.scalars().first()
        if not channel:
            return default
        return (channel.meta or {}).get(key, default)


async def set_meta_key(channel_id: str, key: str, value) -> bool:
    """Escriu una clau de channel.meta preservant la resta (config inclosa).
    value=None elimina la clau."""
    async with get_async_db_context() as db:
        result = await db.execute(select(Channel).filter(Channel.id == channel_id))
        channel = result.scalars().first()
        if not channel:
            return False
        meta = {**(channel.meta or {})}
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
        channel.meta = meta
        await db.commit()
        return True


############################
# Tauler de tasques
############################


async def get_tasks(channel_id: str) -> list[dict]:
    tasks = await get_meta_key(channel_id, "collab_tasks", [])
    return tasks if isinstance(tasks, list) else []


async def create_task(channel_id: str, title: str, created_by: str = "", assignee: str = "") -> dict:
    task = {
        "id": secrets.token_hex(3),
        "title": title.strip(),
        "status": "pending",
        "assignee": assignee.strip(),
        "notes": "",
        "created_by": created_by,
    }
    tasks = await get_tasks(channel_id)
    tasks.append(task)
    await set_meta_key(channel_id, "collab_tasks", tasks)
    return task


async def update_task(
    channel_id: str,
    task_id: str,
    title: str = "",
    status: str = "",
    assignee: str = "",
    notes: str = "",
) -> tuple[bool, str]:
    """Actualitza els camps no buits d'una tasca. Retorna (ok, motiu)."""
    if status and status not in TASK_STATUSES:
        return False, f"Estat invàlid: {status} (vàlids: {', '.join(TASK_STATUSES)})"
    tasks = await get_tasks(channel_id)
    for task in tasks:
        if task.get("id") == task_id:
            if title:
                task["title"] = title.strip()
            if status:
                task["status"] = status
            if assignee:
                task["assignee"] = assignee.strip()
            if notes:
                task["notes"] = notes.strip()
            await set_meta_key(channel_id, "collab_tasks", tasks)
            return True, "Tasca actualitzada."
    return False, f"No existeix cap tasca amb id {task_id}"


async def delete_task(channel_id: str, task_id: str) -> bool:
    tasks = await get_tasks(channel_id)
    remaining = [t for t in tasks if t.get("id") != task_id]
    if len(remaining) == len(tasks):
        return False
    await set_meta_key(channel_id, "collab_tasks", remaining)
    return True


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


async def get_summary(channel_id: str) -> str:
    return await get_meta_key(channel_id, "collab_summary", "") or ""


async def set_summary(channel_id: str, summary: str) -> bool:
    return await set_meta_key(channel_id, "collab_summary", summary.strip() or None)


############################
# Fase de treball de l'equip (filosofia: primer planificar, després executar)
############################

PHASES = ("planning", "execution")


async def get_phase(channel_id: str) -> str:
    phase = await get_meta_key(channel_id, "collab_phase", "planning")
    return phase if phase in PHASES else "planning"


async def set_phase(channel_id: str, phase: str) -> bool:
    return await set_meta_key(channel_id, "collab_phase", phase if phase in PHASES else "planning")


############################
# Agents caiguts (sense tokens, timeouts, errors) — visible per a tot l'equip
############################


async def get_down_agents(channel_id: str) -> dict:
    down = await get_meta_key(channel_id, "collab_down_agents", {})
    return down if isinstance(down, dict) else {}


async def set_down_agent(channel_id: str, agent_id: str, reason: str) -> bool:
    down = await get_down_agents(channel_id)
    down[agent_id] = {"reason": reason, "since": int(time.time())}
    return await set_meta_key(channel_id, "collab_down_agents", down)


async def clear_down_agent(channel_id: str, agent_id: str) -> bool:
    down = await get_down_agents(channel_id)
    if agent_id not in down:
        return False
    down.pop(agent_id, None)
    return await set_meta_key(channel_id, "collab_down_agents", down or None)


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
