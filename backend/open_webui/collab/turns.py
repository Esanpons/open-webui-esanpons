"""Gestió del cicle de vida i la cancel·lació dels torns col·laboratius."""

from open_webui.collab.config import CollabConfig
from open_webui.models.messages import MessageForm, Messages


_turn_cancellables: dict[str, dict] = {}


def active_turn_id(channel_id: str) -> str | None:
    for turn_id, info in _turn_cancellables.items():
        if info.get("channel_id") == channel_id:
            return turn_id
    return None


async def cancel_turn(
    channel_id: str, turn_id: str | None = None, reason: str = "user_requested"
) -> bool:
    """Cancel·la només el torn actiu indicat (o l'actiu del canal)."""
    turn_id = turn_id or active_turn_id(channel_id)
    info = _turn_cancellables.get(turn_id) if turn_id else None
    if not info or info.get("channel_id") != channel_id:
        return False
    task = info.get("task")
    if not task or task.done():
        return False
    info["cancel_reason"] = reason
    if info.get("tool_lock_depth", 0) > 0:
        info["cancel_pending"] = True
        return False
    task.cancel()
    return True


def lock_turn_tool(turn_id: str, tool: str) -> bool:
    """Protegeix una operació amb efectes contra cancel·lacions a mig executar."""
    info = _turn_cancellables.get(turn_id)
    if not info:
        return False
    info["tool_lock_depth"] = int(info.get("tool_lock_depth", 0)) + 1
    info["active_tool"] = tool
    return True


def unlock_turn_tool(turn_id: str) -> bool:
    """Allibera el lock i aplica una cancel·lació que hagués quedat pendent."""
    info = _turn_cancellables.get(turn_id)
    if not info:
        return False
    depth = max(0, int(info.get("tool_lock_depth", 0)) - 1)
    info["tool_lock_depth"] = depth
    if depth:
        return False
    info["active_tool"] = None
    if not info.pop("cancel_pending", False):
        return False
    task = info.get("task")
    if task and not task.done():
        task.cancel()
        return True
    return False


def _effective_turn_timeout(config: CollabConfig) -> int | None:
    configured = int(config.guardrail("turn_timeout") or 0)
    return configured if configured > 0 else None


async def cleanup_orphan_turn_messages(channel_id: str, *, limit: int = 50) -> int:
    """Tanca els placeholders de torn orfes ("⏳ *treballant…*" amb done=False).

    Després d'un crash a mig torn, aquests missatges queden penjats i la UI
    mostra un agent "parlant" per sempre. En arrencar una ronda nova es marquen
    com a interromputs. Retorna quants se n'han netejat.

    Nota: no toquem missatges d'un torn REALMENT actiu perquè aquesta funció
    només es crida en adquirir el lease (moment en què no hi ha cap torn viu
    d'aquest worker per aquest canal).
    """
    import logging

    log = logging.getLogger(__name__)
    cleaned = 0
    try:
        messages = await Messages.get_messages_by_channel_id(channel_id, 0, limit)
    except Exception:
        log.warning("No s'han pogut llegir els missatges de %s per netejar orfes",
                    channel_id, exc_info=True)
        return 0
    for message in messages:
        meta = message.meta or {}
        if meta.get("model_id") and not meta.get("done"):
            try:
                await _mark_cancelled_message(message.id, "interrupted")
                cleaned += 1
            except Exception:
                log.warning("No s'ha pogut netejar el placeholder orfe %s",
                            message.id, exc_info=True)
    if cleaned:
        log.info("Netejats %d placeholders de torn orfes al canal %s", cleaned, channel_id)
    return cleaned


async def _mark_cancelled_message(message_id: str, reason: str) -> None:
    labels = {
        "user_requested": "cancel·lat per l'usuari",
        "timeout": "cancel·lat per timeout",
        "preempted": "cancel·lat per un missatge nou",
        "lease_lost": "interromput en perdre el lease",
        "interrupted": "interromput (reinici del servidor)",
    }
    label = labels.get(reason, f"cancel·lat ({reason})")
    message = await Messages.get_message_by_id(message_id)
    # Conserva el text parcial que l'agent havia arribat a escriure: tallar un
    # torn no ha de fer perdre la feina ja transmesa (el placeholder
    # "⏳ *treballant…*" sí que se substitueix sencer).
    partial = (message.content or "").strip() if message else ""
    if partial.startswith("⏳"):
        partial = ""
    notice = f"⚠️ Torn {label}."
    content = f"{partial}\n\n{notice}" if partial else notice
    await Messages.update_message_by_id(
        message_id,
        MessageForm(
            content=content,
            data=(message.data if message else {}) or {},
            meta={"done": True, "cancelled": True, "cancel_reason": reason},
        ),
    )
