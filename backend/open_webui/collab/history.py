"""Historial complet de la conversa de l'espai, consultable pels agents.

Tota la conversa queda guardada per sempre a la taula `message` del canal; el
context normal dels torns és una finestra (guardarail `context_messages`), i
quan un agent (o l'usuari via un agent) necessita revisar-ho TOT o buscar una
cosa antiga, fa servir aquestes funcions via les eines `read_conversation` /
`search_conversation` del tool collab.
"""

import json
import logging

from open_webui.collab.files import escape_like
from open_webui.internal.db import get_async_db_context
from open_webui.models.messages import Message
from open_webui.models.users import Users
from sqlalchemy import func, select

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1200  # per missatge, per no rebentar el context de l'agent


def _meta(row) -> dict:
    meta = row.meta
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    return meta or {}


async def _label_rows(rows) -> list[str]:
    user_ids = list({r.user_id for r in rows if not _meta(r).get("model_id")})
    users = {u.id: u for u in await Users.get_users_by_user_ids(user_ids)} if user_ids else {}
    lines = []
    for r in rows:
        meta = _meta(r)
        content = (r.content or "").strip()
        if not content:
            continue
        if meta.get("model_id"):
            author = meta.get("model_name") or meta.get("model_id")
        else:
            u = users.get(r.user_id)
            author = u.name if u else "Usuari"
        if len(content) > MAX_MESSAGE_CHARS:
            content = content[:MAX_MESSAGE_CHARS] + " […missatge tallat…]"
        lines.append(f"[{author}]: {content}")
    return lines


async def count_messages(channel_id: str) -> int:
    async with get_async_db_context() as db:
        result = await db.execute(
            select(func.count()).select_from(Message).filter(Message.channel_id == channel_id)
        )
        return int(result.scalar() or 0)


async def conversation_text(channel_id: str, offset: int = 0, limit: int = 60) -> str:
    """Tros de la conversa en ordre cronològic. offset = quants missatges
    RECENTS se salten (0 = els últims `limit`). Inclou una capçalera amb el
    total perquè l'agent sàpiga on és."""
    limit = max(1, min(int(limit or 60), 200))
    offset = max(0, int(offset or 0))
    total = await count_messages(channel_id)

    async with get_async_db_context() as db:
        result = await db.execute(
            select(Message)
            .filter(Message.channel_id == channel_id)
            .order_by(Message.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list(result.scalars().all())[::-1]  # cronològic

    lines = await _label_rows(rows)
    if not lines:
        return f"(cap missatge en aquest tram; la conversa té {total} missatges en total)"
    header = (
        f"Conversa del canal — {total} missatges en total; mostrant {len(lines)} "
        f"(saltant els {offset} més recents):\n\n"
    )
    return header + "\n\n".join(lines)


async def search_conversation(channel_id: str, query: str, limit: int = 20) -> str:
    """Cerca (case-insensitive) al text de tota la conversa del canal."""
    query = (query or "").strip()
    if not query:
        return "Indica què vols buscar."
    limit = max(1, min(int(limit or 20), 50))

    async with get_async_db_context() as db:
        result = await db.execute(
            select(Message)
            .filter(Message.channel_id == channel_id)
            .filter(Message.content.ilike(f"%{escape_like(query)}%", escape="\\"))
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())[::-1]

    lines = await _label_rows(rows)
    if not lines:
        return f"Cap missatge conté «{query}»."
    return f"Missatges que contenen «{query}» ({len(lines)}):\n\n" + "\n\n".join(lines)
