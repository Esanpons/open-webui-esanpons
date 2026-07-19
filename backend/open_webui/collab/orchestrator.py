"""Orquestrador de la taula rodona: rondes, hand-raising i torns d'agent.

Filosofia (vegeu docs/plans/espai-collaboratiu.md): cap agent director. Després
de cada missatge es pregunta a cada agent si vol intervenir (hand-raising) i
parlen per ordre de prioritat, un torn seqüencial cada vegada, fins que ningú
vol afegir res (consens implícit) o un guardarail configurable atura la ronda.

W7 refactor: torns → turns.py, prompts → prompts.py, context → context.py,
agents_status → agents_status.py, voting → voting.py.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import suppress
from typing import Optional

from starlette.responses import Response, StreamingResponse

from open_webui.collab.config import CollabConfig, get_collab_config
from open_webui.collab.backpressure import acquire as acquire_model_slot
from open_webui.collab.budget import _is_degraded, check_budget
from open_webui.collab.circuit_breaker import can_proceed, record_failure, record_success
from open_webui.collab.engine import (
    acquire_lease,
    list_events,
    reconcile_expired_session,
    record_user_message,
    release_lease,
    renew_lease,
    transition_receipt,
)
from open_webui.collab.file_tools import COLLAB_TOOL_ID, ensure_collab_tool
from open_webui.collab.profiles import get_channel_config, resolve_agent
from open_webui.collab.files import (
    diff_snapshots,
    format_changes,
    snapshot,
    tree_as_text,
)
from open_webui.collab.tasks import (
    clear_down_agent,
    clear_end_proposal,
    get_down_agents,
    get_end_proposal,
    get_phase,
    get_summary,
    get_tasks,
    set_down_agent,
    set_end_proposal,
    set_phase,
    set_summary,
    tasks_as_text,
)
from open_webui.collab.usage import (
    STATUS_SUCCESS,
    classify_error,
    estimate_tokens,
    record_usage,
)

# W7 — mòduls extrets
from open_webui.collab.turns import (
    _effective_turn_timeout,
    _mark_cancelled_message,
    _turn_cancellables,
    active_turn_id,
    cancel_turn,
    cleanup_orphan_turn_messages,
    lock_turn_tool,
    unlock_turn_tool,
)
from open_webui.collab.prompts import (
    SYSTEM_AUTHOR,
    _PHILOSOPHY,
    _apply_agent_prompt,
    _model_supports_effort,
    _phase_block,
)
from open_webui.collab.context import (
    _board_text,
    _collab_ctx,
    collab_generation_context,
    _participants_line,
    _project_block,
    build_transcript,
    project_tree_text,
)
from open_webui.collab.agents_status import (
    _RETRY_DOWN_SECONDS,
    extract_model_error,
    _mark_agent_down,
    _mark_agent_up,
)
from open_webui.collab.voting import _update_summary, _vote_on_proposal

from open_webui.models.channels import ChannelModel, Channels
from open_webui.models.messages import MessageForm, Messages
from open_webui.models.users import Users
from open_webui.socket.main import sio
from open_webui.utils.channels import replace_mentions
from open_webui.utils.models import get_all_models, get_filtered_models

log = logging.getLogger(__name__)

# Estat en memòria de les rondes actives: channel_id -> {"stop": bool}.
# (Un sol worker; si mai es desplega multi-worker caldrà moure-ho a Redis.)
_active_rounds: dict[str, dict] = {}

_HANDRAISE_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
_INTERVENE_RE = re.compile(r'"intervene"\s*:\s*(true|false)', re.IGNORECASE)
_PRIORITY_RE = re.compile(r'"priority"\s*:\s*(\d+)')

# Marcadors de text per a les propostes de consens — funcionen amb QUALSEVOL
# model, també els pipes CLI que no fan tool-calling (l'eina propose_finish és
# l'alternativa per als models amb tools natius).
#
# Ancorats a INICI DE LÍNIA (MULTILINE): el prompt demana que el missatge
# ACABI amb el marcador, així que només el compta si obre una línia. Sense
# l'àncora, citar-lo enmig d'una frase ("recordeu escriure FEINA_ACABADA:")
# disparava una proposta de tancament espúria. Mateix problema que ja es va
# corregir amb la detecció d'errors (vegeu agents_status.py).
_FINISH_MARKER_RE = re.compile(r"^\s*FEINA_ACABADA\s*:?\s*(.*)", re.DOTALL | re.MULTILINE)
_PLAN_MARKER_RE = re.compile(r"^\s*PLA_ACORDAT\s*:?\s*(.*)", re.DOTALL | re.MULTILINE)
_WAIT_USER_MARKER = "ESPEREM_USUARI"

# Errors consecutius de mà alçada abans de declarar l'agent caigut (in-memory).
_handraise_failures: dict[tuple[str, str], int] = {}
_budget_notices: dict[str, str] = {}
# model_id -> timestamp en què es va detectar que no admet tool calling. Amb TTL
# perquè un model que guanyi suport de tools (canvi de proveïdor/versió) es
# torni a provar en lloc de quedar vetat fins a reiniciar el procés.
_models_without_collab_tools: dict[str, float] = {}
_TOOLS_UNSUPPORTED_TTL = 3600  # 1h


def _model_lacks_tools(model_id: str) -> bool:
    """El model està marcat com a sense suport de tools i encara dins del TTL?"""
    ts = _models_without_collab_tools.get(model_id)
    if ts is None:
        return False
    if time.time() - ts > _TOOLS_UNSUPPORTED_TTL:
        _models_without_collab_tools.pop(model_id, None)
        return False
    return True

# Sentinella retornat per _quick_completion quan la crida NO s'ha fet perquè el
# pressupost la bloqueja (estat administratiu), a diferència d'un None que
# significa "ha fallat". Handraise el tracta com a "pass", no com a "error":
# així un agent sa amb pressupost exhaurit no es marca com a caigut.
BUDGET_BLOCKED = object()

# Les crides auxiliars han de ser realment curtes. Sense max_tokens alguns
# proveïdors reserven milers de tokens de sortida per un JSON de tres camps i
# rebutgen la petició per TPM abans de començar a generar.
_QUICK_TASK_MAX_TOKENS = {
    # Els models de raonament compten els tokens interns dins el límit; 1024
    # continua sent molt menys que els 7k–8k reservats abans, però evita tallar
    # el JSON de Nemotron/GPT-OSS abans que acabi de raonar.
    "handraise": 1024,
    "vote": 1024,
    "summary": 1200,
}
# Sostre de sanitat del poll de generació (segons sense cap progrés de contingut)
# INDEPENDENT del guardrail turn_timeout: evita que un pipe mort deixi el bucle
# girant per sempre retenint el slot de backpressure quan turn_timeout=0.
_STALLED_GENERATION_TIMEOUT = 300

_RETRY_AFTER_RE = re.compile(
    r"(?:please\s+try\s+again\s+in|retry\s+after)\s+([0-9]+(?:\.[0-9]+)?)\s*s",
    re.IGNORECASE,
)
# El model no admet tool calling. Els proveïdors ho diuen de formes diferents
# ("Error: `tool calling` is not supported", "`tool calling` is not supported
# with this model", "does not support tools"...). No exigim prefix ni posició.
_TOOL_CALLING_UNSUPPORTED_RE = re.compile(
    r"`?tool[ _]?call(?:ing|s)?`?\s+(?:is|are)\s+not\s+supported"
    r"|does\s+not\s+support\s+tool"
    r"|no\s+support\s+for\s+tool",
    re.IGNORECASE,
)
# L'error parla de mida/TOKENS (no de peticions): reintentar amb el mateix
# prompt tornaria a fallar; cal reduir-lo. Cobreix les variants de Groq
# ("Request Entity Too Large", codi `request_too_large`), OpenAI/OpenRouter
# (TPM, context length) i altres proveïdors.
_TOKEN_LIMIT_HINT_RE = re.compile(
    r"tokens per minute|\bTPM\b|request[ _]?(?:entity[ _]?)?too[ _]?large"
    r"|payload too large|\b413\b|prompt is too long"
    r"|reduce the length|context.?(?:length|window)|input is too long",
    re.IGNORECASE,
)


def _retry_after_seconds(content: str | None) -> float | None:
    """Retard curt sol·licitat pel proveïdor dins un error de rate-limit."""
    if not content or "rate limit" not in content.lower():
        return None
    match = _RETRY_AFTER_RE.search(content)
    if not match:
        return None
    return min(15.0, max(0.0, float(match.group(1))))


async def _reset_response_for_retry(message_id: str) -> None:
    """Reutilitza el placeholder del torn després d'una fallada recuperable."""
    message = await Messages.get_message_by_id(message_id)
    if not message:
        return
    await Messages.update_message_by_id(
        message_id,
        MessageForm(
            content="",
            data=message.data or {},
            meta={**(message.meta or {}), "done": False},
        ),
    )


async def _resolved_agent(channel_id: str, agent_id: str) -> dict:
    """Resol els overrides efectius; en absència de perfil conserva el comportament base."""
    try:
        channel_config = await get_channel_config(channel_id)
        overrides = (channel_config or {}).get("agent_overrides") or []
        return resolve_agent(agent_id, overrides)
    except Exception:
        log.exception("No s'han pogut resoldre els overrides de %s", agent_id)
        return resolve_agent(agent_id, [])


def _agent_display_name(resolved: dict, model: dict | None, agent_id: str) -> str:
    """Nom a mostrar per un agent: display_name del override > nom del model > id."""
    dn = resolved.get("display_name")
    if dn:
        return dn
    if model:
        return model.get("name", agent_id)
    return agent_id


async def _channel_budget(channel_id: str) -> dict | None:
    try:
        channel_config = await get_channel_config(channel_id)
        return (channel_config or {}).get("budget")
    except Exception:
        log.exception("No s'ha pogut carregar el pressupost de %s", channel_id)
        return None


async def _circuit_allows(channel_id: str, agent_id: str) -> bool:
    """El circuit protegeix el proveïdor, però una fallada d'estat no bloqueja torns."""
    try:
        return await can_proceed(channel_id, agent_id)
    except Exception:
        log.exception("No s'ha pogut consultar el circuit de %s", agent_id)
        return True


async def _record_circuit_result(
    channel_id: str, agent_id: str, status: str
) -> None:
    try:
        if status == STATUS_SUCCESS:
            await record_success(channel_id, agent_id)
        else:
            await record_failure(channel_id, agent_id, status)
    except Exception:
        log.exception("No s'ha pogut actualitzar el circuit de %s", agent_id)


async def _effective_collab_config(channel: ChannelModel) -> CollabConfig:
    """Retorna la configuració canònica que consumeix el motor.

    Aplicar o editar una plantilla ja sincronitza la seva configuració a
    ``channel.meta.collab``.  ``collab_channel_config`` conserva la vinculació,
    les personalitzacions dels agents i el pressupost, però una còpia antiga
    d'aquest registre no pot desactivar una taula que el panell mostra activa.
    """
    return get_collab_config(channel)


async def _budget_model_or_none(
    request,
    channel,
    user,
    agent_id: str,
    call_type: str,
    resolved: dict,
) -> str | None:
    decision = await check_budget(
        channel.id, agent_id, call_type, await _channel_budget(channel.id)
    )
    if decision.allowed:
        _budget_notices.pop(channel.id, None)
        return agent_id
    if decision.action == "downgrade" and resolved.get("fallback_model_id"):
        return str(resolved["fallback_model_id"])
    if decision.action == "stop":
        request_stop(channel.id)
    reason = decision.reason or "Pressupost exhaurit"
    if _budget_notices.get(channel.id) != reason:
        _budget_notices[channel.id] = reason
        icon = "🛑" if decision.action == "stop" else "⏸️"
        await post_notice(request, channel, user, f"{icon} {reason}.")
    return None


def _content_from_completion_payload(payload) -> str:
    """Normalitza les formes habituals OpenAI/Responses/pipes a text."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if choices:
        choice = choices[0] or {}
        content = (choice.get("message") or {}).get("content")
        if content is None:
            content = (choice.get("delta") or {}).get("content")
        if isinstance(content, str):
            return content
    texts = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if texts:
        return "\n\n".join(texts)
    for key in ("content", "response", "text"):
        if isinstance(payload.get(key), str):
            return payload[key]
    return ""


def _completion_error(payload, status_code: int | None = None) -> str | None:
    if isinstance(payload, dict) and payload.get("error"):
        error = payload["error"]
        if isinstance(error, dict):
            # OpenRouter (i altres agregadors) amaguen la causa real dins
            # error.code i error.metadata (provider_name, raw); sense això el
            # missatge queda en un "Provider returned error" inservible.
            parts = [str(error.get("message", error.get("detail", error)))]
            code = error.get("code") or error.get("status")
            if code:
                parts.append(f"[codi {code}]")
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                provider = metadata.get("provider_name")
                if provider:
                    parts.append(f"[proveïdor: {provider}]")
                raw = metadata.get("raw")
                if raw:
                    parts.append(str(raw)[:300])
            return " ".join(parts)
        return str(error)
    if status_code is not None and status_code >= 400:
        return f"Provider returned HTTP {status_code}"
    return None


def _decode_completion_bytes(raw: bytes | str) -> tuple[str, dict | None]:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text.strip(), None
    return _content_from_completion_payload(payload), payload if isinstance(payload, dict) else None


async def _normalize_completion_response(response) -> tuple[str, dict]:
    """Retorna ``(content, payload)`` per dict, Response i SSE/NDJSON.

    Alguns pipes retornen JSONResponse o PlainTextResponse encara que el
    caller hagi demanat JSON. Les crides col·laboratives no poden assumir
    subscripció ``response['choices']``.
    """
    if isinstance(response, dict):
        error = _completion_error(response)
        if error:
            raise RuntimeError(error)
        return _content_from_completion_payload(response), response
    if isinstance(response, StreamingResponse):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk))
        content_parts = []
        last_payload = {}
        for line in "".join(chunks).splitlines():
            part = line.removeprefix("data:").strip()
            if not part or part == "[DONE]":
                continue
            content, payload = _decode_completion_bytes(part)
            if payload:
                last_payload = payload
                error = _completion_error(payload, response.status_code)
                if error:
                    raise RuntimeError(error)
            if content:
                content_parts.append(content)
        return "".join(content_parts), last_payload
    if isinstance(response, Response):
        content, payload = _decode_completion_bytes(response.body)
        error = _completion_error(payload, response.status_code)
        if error:
            raise RuntimeError(error)
        return content, payload or {}
    raise TypeError(f"Unsupported completion response: {type(response).__name__}")


async def _run_generation_until_done(request, form_data, user, message_id: str):
    response = await request.app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)
    # Cridat fora d'una ruta ASGI, ningú consumeix automàticament el body d'un
    # StreamingResponse. Consumir-lo és el que executa el pipeline, emet els
    # deltes i marca el missatge del canal com a acabat.
    direct_content = ""
    if isinstance(response, StreamingResponse):
        direct_content, _ = await _normalize_completion_response(response)
    elif isinstance(response, (dict, Response)):
        direct_content, _ = await _normalize_completion_response(response)
    message = await Messages.get_message_by_id(message_id)
    if message and not (message.meta or {}).get("done") and direct_content.strip():
        await Messages.update_message_by_id(
            message_id,
            MessageForm(
                content=direct_content,
                data=message.data or {},
                meta={**(message.meta or {}), "done": True},
            ),
        )
    # Sostre de sanitat: fins i tot amb turn_timeout=0 (sense timeout de torn),
    # aquest bucle no pot girar per sempre si el pipeline mor sense marcar
    # `done`. Es talla si el contingut no progressa durant massa temps; qualsevol
    # canvi (delta de streaming) reinicia el rellotge, així que un torn lent que
    # va escrivint no es veu afectat.
    last_content = None
    last_progress = time.monotonic()
    while True:
        message = await Messages.get_message_by_id(message_id)
        if not message or (message.meta or {}).get("done"):
            persisted = message.content if message else None
            return persisted if persisted and persisted.strip() else (direct_content or persisted)
        current = message.content or ""
        if current != last_content:
            last_content = current
            last_progress = time.monotonic()
        elif time.monotonic() - last_progress > _STALLED_GENERATION_TIMEOUT:
            log.warning(
                "Generació encallada (%ss sense progrés) al missatge %s; s'abandona el poll",
                _STALLED_GENERATION_TIMEOUT,
                message_id,
            )
            return current.strip() or direct_content or current
        await asyncio.sleep(1.5)


async def _record_usage_safely(
    channel_id: str, agent_id: str, call_type: str, *, model_id: str | None = None, **kwargs
):
    """La telemetria mai ha de fer fallar una conversa."""
    try:
        if model_id and "estimated_cost" not in kwargs:
            from open_webui.collab.budget import estimate_cost

            kwargs["estimated_cost"] = estimate_cost(
                model_id, kwargs.get("input_tokens"), kwargs.get("output_tokens")
            )
        await record_usage(channel_id, agent_id, call_type, **kwargs)
    except Exception:
        log.warning(
            "No s'ha pogut registrar telemetria %s per %s al canal %s",
            call_type,
            agent_id,
            channel_id,
            exc_info=True,
        )


def _response_usage(response: dict) -> tuple[int | None, int | None]:
    """Extreu usage OpenAI-compatible; si no hi és, el caller farà estimació."""
    usage = response.get("usage") or {}
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    return input_tokens, output_tokens


async def _current_phase(channel_id: str, config: CollabConfig) -> str:
    if not config.guardrail("require_planning"):
        return "free"
    return await get_phase(channel_id)


def is_round_active(channel_id: str) -> bool:
    return channel_id in _active_rounds


def request_stop(channel_id: str) -> bool:
    state = _active_rounds.get(channel_id)
    if state:
        state["stop"] = True
        return True
    return False


async def handle_collab_message(request, channel, message, user) -> bool:
    """Punt d'entrada des del hook de channels.py. Retorna True si el missatge
    l'ha gestionat el mode col·laboratiu (i per tant NO ha de passar pel
    model_response_handler estàndard)."""
    content = (message.content or "").strip()

    if content.lower().startswith("/collab"):
        from open_webui.collab.commands import handle_command

        await handle_command(request, channel, message, user)
        return True

    config = get_collab_config(channel)

    # Auto-activació: si la taula té agents configurats però ningú ha premut
    # "Activa", el PRIMER missatge humà del canal la posa en marxa sol.
    if config.agents and not config.enabled and not (message.meta or {}).get("model_id"):
        from open_webui.collab.config import save_collab_config
        from open_webui.collab.history import count_messages

        if await count_messages(channel.id) <= 2:
            config.enabled = True
            await save_collab_config(channel.id, config)
            await post_notice(
                request,
                channel,
                user,
                "🤝 Espai col·laboratiu activat automàticament — l'equip es posa en marxa.",
            )
            event_seq = await _persist_user_message(channel.id, message, config.agents)
            await run_round(request, channel, user, event_seq=event_seq)
            return True

    if not (config.enabled and config.agents):
        return False

    if (message.meta or {}).get("model_id"):
        # Missatge generat per un agent: ja el veu la ronda en curs.
        return True

    event_seq = await _persist_user_message(channel.id, message, config.agents)
    await run_round(request, channel, user, event_seq=event_seq)
    return True


async def _persist_user_message(channel_id: str, message, agents: list[str]):
    """Registra l'entrada humana i els receipts abans d'engegar la ronda.

    Les dues escriptures comparteixen una transacció interna a ``engine`` per
    garantir ordre monotònic; la creació de receipts és idempotent.
    """
    message_id = str(message.id) if getattr(message, "id", None) is not None else None
    event = await record_user_message(
        channel_id,
        agents,
        message_id=message_id,
        payload={"content_length": len(message.content or "")},
    )
    await _emit_collab_event(event)
    return event.seq


async def _latest_user_event_seq(channel_id: str, since: int) -> tuple[int | None, int]:
    """Retorna l'última entrada humana i el cursor escanejat.

    Es pagina per no perdre una entrada encara que entre torns s'hagin generat
    més de 1.000 events. El scheduler continu només ho consulta als límits
    segurs entre torns.
    """
    cursor = since
    latest = None
    while True:
        events = await list_events(channel_id, since=cursor, limit=1000)
        if not events:
            break
        for event in events:
            cursor = max(cursor, event.seq)
            if event.type == "user_message":
                latest = event.seq
        if len(events) < 1000:
            break
    return latest, cursor


async def _emit_collab_event(event):
    """Publica un event persistent al mateix room socket que els missatges."""
    try:
        await sio.emit(
            "events:channel",
            {
                "channel_id": event.channel_id,
                "message_id": event.message_id,
                "data": {
                    "type": "collab_event.v1",
                    "data": {
                        "seq": event.seq,
                        "event": {
                            "type": event.type,
                            "agent_id": event.agent_id,
                            "message_id": event.message_id,
                            "payload": event.payload or {},
                            "status": event.status,
                            "timestamp": event.created_at,
                        },
                    },
                },
            },
            to=f"channel:{event.channel_id}",
        )
    except Exception:
        log.warning(
            "No s'ha pogut emetre l'event collab %s/%s",
            event.channel_id,
            event.seq,
            exc_info=True,
        )


async def _transition_receipt(channel_id: str, event_seq: int, agent_id: str, state: str):
    event, _summary = await transition_receipt(
        channel_id, event_seq, agent_id, state
    )
    if event is not None:
        await _emit_collab_event(event)


async def _emit_turn_event(
    channel_id: str, event_type: str, agent_id: str, message_id: str | None, payload: dict
):
    """Event persistent de cicle de torn (turn_started/turn_finished) per a la
    barra d'agents (W1: estat «speaking»). Mai ha de fer fallar un torn."""
    try:
        from open_webui.collab.engine import append_event

        event = await append_event(
            channel_id,
            event_type,
            agent_id=agent_id,
            message_id=message_id,
            payload=payload,
        )
        await _emit_collab_event(event)
    except Exception:
        log.warning(
            "No s'ha pogut emetre l'event %s de %s al canal %s",
            event_type,
            agent_id,
            channel_id,
            exc_info=True,
        )


async def _renew_round_lease(channel_id: str, owner: str, state: dict):
    """Manté el lease viu; si es perd, demana una sortida neta de la ronda.

    Un error transitori de BD no ha de matar aquesta tasca en silenci (deixaria
    la ronda viva sense renovar el lease → un altre worker podria adquirir-lo i
    executar una segona ronda al mateix canal). Es tolera un nombre limitat
    d'errors consecutius abans de declarar el lease perdut.
    """
    consecutive_errors = 0
    while not state["stop"]:
        await asyncio.sleep(10)
        try:
            renewed = await renew_lease(channel_id, owner)
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            log.warning(
                "Error renovant el lease del canal %s (%d consecutius)",
                channel_id,
                consecutive_errors,
                exc_info=True,
            )
            # Amb TTL 30s i renovació cada 10s, tolerem 2 errors (20s) i encara
            # queda marge; al tercer donem el lease per perdut.
            if consecutive_errors < 3:
                continue
            renewed = False

        if not renewed:
            log.error("S'ha perdut el lease persistent del canal %s", channel_id)
            state["lease_lost"] = True
            state["stop"] = True
            # Un altre worker pot agafar el canal ara mateix: tallar el torn en
            # curs evita dos torns simultanis sobre el mateix canal/projecte.
            with suppress(Exception):
                await cancel_turn(channel_id, reason="lease_lost")
            return


async def reconcile_channel(channel_id: str) -> bool:
    """Recupera una sessió persistent abandonada després d'una caiguda."""
    if channel_id in _active_rounds:
        return False
    return await reconcile_expired_session(channel_id)


async def post_notice(request, channel, user, content: str):
    """Publica un missatge informatiu de la taula rodona al canal."""
    from open_webui.routers.channels import new_message_handler

    try:
        await new_message_handler(
            request,
            channel.id,
            MessageForm(content=content, meta={**SYSTEM_AUTHOR, "done": True}),
            user,
            None,
        )
    except Exception:
        log.exception("No s'ha pogut publicar l'avís al canal %s", channel.id)


async def _get_models(request, user) -> dict:
    return {
        model["id"]: model
        for model in await get_filtered_models(await get_all_models(request, user=user), user)
    }


async def _quick_completion(
    request, user, channel, config: CollabConfig, agent_id: str, system: str, prompt: str, task: str,
    *, budget_sentinel: bool = False,
):
    """Crida curta no-streaming a un agent (mà alçada, vot, resum). Retorna el
    contingut de la resposta o None si falla o supera el handraise_timeout.

    Amb ``budget_sentinel=True`` retorna ``BUDGET_BLOCKED`` (en comptes de None)
    quan la crida no s'ha fet perquè el pressupost la bloqueja, perquè el caller
    ho pugui distingir d'una fallada real. La resta de callers (vot, resum)
    reben None i ho tracten com "sense resposta"."""
    from open_webui.utils.chat import generate_chat_completion

    if not await _circuit_allows(channel.id, agent_id):
        log.info("Circuit obert: s'omet la crida %s de %s", task, agent_id)
        return None
    resolved = await _resolved_agent(channel.id, agent_id)
    effective_model_id = await _budget_model_or_none(
        request, channel, user, agent_id, task, resolved
    )
    if effective_model_id is None:
        return BUDGET_BLOCKED if budget_sentinel else None
    resolved_name = _agent_display_name(resolved, None, agent_id)
    system = _apply_agent_prompt(system, resolved, resolved_name)
    turn_id = str(uuid.uuid4())
    form_data = {
        "model": effective_model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "metadata": {"collab": {**_collab_ctx(channel, config), "task": task}},
    }
    if resolved.get("token_limit"):
        form_data["max_tokens"] = int(resolved["token_limit"])
    elif task in _QUICK_TASK_MAX_TOKENS:
        form_data["max_tokens"] = _QUICK_TASK_MAX_TOKENS[task]

    timeout = int(config.guardrail("handraise_timeout") or 0)
    estimated_input = estimate_tokens(system) + estimate_tokens(prompt)
    for attempt in range(2):
        try:
            async with acquire_model_slot(effective_model_id):
                coroutine = generate_chat_completion(
                    request, form_data, user, bypass_filter=True
                )
                response = await (
                    asyncio.wait_for(coroutine, timeout) if timeout else coroutine
                )
            content, payload = await _normalize_completion_response(response)
            input_tokens, output_tokens = _response_usage(payload)
            status, detail = (STATUS_SUCCESS, None) if content else classify_error("")
            await _record_usage_safely(
                channel.id,
                agent_id,
                task,
                model_id=effective_model_id,
                input_tokens=input_tokens if input_tokens is not None else estimated_input,
                output_tokens=(
                    output_tokens if output_tokens is not None else estimate_tokens(content)
                ),
                status=status,
                error_detail=detail,
            )
            await _record_circuit_result(channel.id, agent_id, status)
            return content
        except asyncio.TimeoutError as exc:
            log.warning(
                "Crida %s de %s ha superat el timeout (%ss)", task, agent_id, timeout
            )
            status, detail = classify_error(exc)
            await _record_usage_safely(
                channel.id,
                agent_id,
                task,
                model_id=effective_model_id,
                input_tokens=estimated_input,
                status=status,
                error_detail=detail,
            )
            await _record_circuit_result(channel.id, agent_id, status)
            return None
        except Exception as exc:
            status, detail = classify_error(exc)
            await _record_usage_safely(
                channel.id,
                agent_id,
                task,
                model_id=effective_model_id,
                input_tokens=estimated_input,
                status=status,
                error_detail=detail,
            )
            retry_delay = _retry_after_seconds(str(exc))
            if attempt == 0 and retry_delay is not None:
                log.warning(
                    "Rate-limit transitori a la crida %s de %s; reintent en %.2fs",
                    task,
                    agent_id,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
                continue
            log.exception("Crida %s de %s ha fallat", task, agent_id)
            await _record_circuit_result(channel.id, agent_id, status)
            return None

    return None


async def _handraise_one(
    request,
    user,
    channel,
    config: CollabConfig,
    models: dict,
    agent_id: str,
    transcript: str,
    board: str,
    phase: str,
) -> tuple[str, Optional[dict]]:
    """Pregunta a un agent si vol intervenir. Retorna (estat, dades):
    ('yes', {'agent','priority','reason'}) | ('no', None) | ('error', None)."""
    model = models.get(agent_id)
    if not model:
        return ("error", None)
    resolved = await _resolved_agent(channel.id, agent_id)
    name = _agent_display_name(resolved, model, agent_id)

    system = (
        f"Ets {name}, membre d'un equip d'IAs que treballa unit en una taula "
        f"rodona amb: {_participants_line(config, models, exclude=agent_id)} i les "
        "persones usuàries.\n\n" + _PHILOSOPHY + _phase_block(phase)
        + _project_block(config) + board
    )
    if phase == "planning":
        question = (
            "Vols intervenir ARA en la PLANIFICACIÓ (pregunta, proposta, objecció, "
            "repartiment de feina, o donar el pla per acordat)?"
        )
    else:
        question = (
            "Vols intervenir ARA (fer la teva part del pla, revisar la feina d'un "
            "altre, anunciar que has acabat, o proposar tancar)? Si estàs ESPERANT "
            "una tasca d'un altre que encara no està feta, NO intervinguis."
        )
    prompt = (
        "Transcripció recent de la taula rodona:\n\n"
        f"{transcript}\n\n"
        f"{question} Si el missatge més recent de l'usuari s'adreça directament "
        "a tu o demana que respongueu TOTS (per exemple: saludeu, presenteu-vos, "
        "opineu tots), marca `intervene: true`: és una petició explícita, no "
        "simple cortesia. No intervinguis per fer-te passar per un altre agent "
        "ni per repetir el que ja s'ha dit. Respon NOMÉS amb aquest JSON, sense res més:\n"
        '{"intervene": true|false, "priority": 1-5, "reason": "màxim una frase"}'
    )

    content = await _quick_completion(
        request, user, channel, config, agent_id, system, prompt, "handraise",
        budget_sentinel=True,
    )
    if content is BUDGET_BLOCKED:
        # Pressupost exhaurit: no és una fallada de l'agent. Es tracta com un
        # "pass" (no intervé) i no compta per marcar-lo caigut.
        return ("budget", None)
    if content is None:
        return ("error", None)

    intervene, priority, reason = None, 3, ""
    for block in _HANDRAISE_JSON_RE.findall(content):
        try:
            data = json.loads(block)
            if "intervene" in data:
                intervene = bool(data.get("intervene"))
                priority = int(data.get("priority") or 3)
                reason = str(data.get("reason") or "")
                break
        except (ValueError, TypeError):
            continue
    if intervene is None:
        match = _INTERVENE_RE.search(content)
        if match:
            intervene = match.group(1).lower() == "true"
            priority_match = _PRIORITY_RE.search(content)
            priority = int(priority_match.group(1)) if priority_match else 3
    if not intervene:
        return ("no", None)
    return (
        "yes",
        {
            "agent": agent_id,
            "priority": max(1, min(5, priority)),
            "profile_priority": int(resolved.get("priority") or 3),
            "reason": reason,
        },
    )


async def handraise(
    request,
    channel,
    config: CollabConfig,
    user,
    models: dict,
    last_speaker: Optional[str],
    event_seq: int | None = None,
) -> tuple[list[str], int, int]:
    """Ronda de mà alçada. Retorna (voluntaris per ordre de prioritat,
    quants han pogut respondre, quants s'han consultat) — així es distingeix
    el consens ("ningú vol parlar") d'una fallada de tots els agents."""
    budget = await _channel_budget(channel.id)
    degraded = await _is_degraded(channel.id, budget)
    # Context (curt) per a la mà alçada: degradat → 5; si no, el de la mà alçada
    # (config.context_messages(handraise=True) ja aplica la semàntica de 0).
    hr_context = 5 if degraded else config.context_messages(handraise=True)
    context_config = config.model_copy(
        update={"guardrails": {**config.guardrails, "context_messages": hr_context}}
    )
    transcript = await build_transcript(channel.id, context_config, models)
    board = await _board_text(channel.id)
    phase = await _current_phase(channel.id, config)

    # Agents caiguts: se salten, excepte quan els toca el reintent automàtic.
    down = await get_down_agents(channel.id)
    now = time.time()
    candidates = []
    for agent_id in config.agents:
        info = down.get(agent_id)
        if info and (now - info.get("since", 0)) < _RETRY_DOWN_SECONDS:
            continue
        candidates.append(agent_id)
    if not config.guardrail("allow_self_reply") and last_speaker in candidates and len(candidates) > 1:
        candidates.remove(last_speaker)

    if event_seq is not None:
        skipped = [agent_id for agent_id in config.agents if agent_id not in candidates]
        for agent_id in skipped:
            await _transition_receipt(channel.id, event_seq, agent_id, "pass")
        for agent_id in candidates:
            await _transition_receipt(channel.id, event_seq, agent_id, "evaluating")

    raw_results = await asyncio.gather(
        *[
            _handraise_one(
                request,
                user,
                channel,
                config,
                models,
                agent_id,
                transcript,
                board,
                phase,
            )
            for agent_id in candidates
        ],
        return_exceptions=True,
    )
    # Una excepció inesperada en una mà alçada no ha de tombar tota la ronda (i
    # amb gather normal, deixaria les altres corrutines sense observar): es
    # tracta com un "error" del seu agent.
    results = []
    for agent_id, res in zip(candidates, raw_results):
        if isinstance(res, BaseException):
            log.warning(
                "Mà alçada de %s ha llançat una excepció inesperada",
                agent_id,
                exc_info=res,
            )
            results.append(("error", None))
        else:
            results.append(res)

    # Comptabilitat de caiguts: 2 errors seguits de mà alçada → caigut; una
    # resposta vàlida → recuperat.
    for agent_id, (status, _payload) in zip(candidates, results):
        if event_seq is not None:
            await _transition_receipt(
                channel.id,
                event_seq,
                agent_id,
                "will_intervene" if status == "yes" else "pass",
            )
        key = (channel.id, agent_id)
        if status == "error":
            _handraise_failures[key] = _handraise_failures.get(key, 0) + 1
            if agent_id in down:
                await set_down_agent(channel.id, agent_id, down[agent_id].get("reason", "error"))
            elif _handraise_failures[key] >= 2:
                await _mark_agent_down(
                    request, channel, user, models, agent_id, "no respon (error o possible límit de quota)"
                )
        else:
            _handraise_failures.pop(key, None)
            if agent_id in down:
                await _mark_agent_up(request, channel, user, models, agent_id)

    volunteers = [payload for status, payload in results if status == "yes"]
    responded = sum(1 for status, _payload in results if status != "error")
    order = {agent_id: idx for idx, agent_id in enumerate(config.agents)}
    volunteers.sort(
        key=lambda v: (
            -v["priority"],
            -v.get("profile_priority", 3),
            order.get(v["agent"], 99),
        )
    )
    return [v["agent"] for v in volunteers], responded, len(candidates)


def _next_agent(agents: list[str], last_speaker: Optional[str]) -> str:
    """Següent agent en rotació (per a les empentes anti-silenci)."""
    if last_speaker in agents and len(agents) > 1:
        return agents[(agents.index(last_speaker) + 1) % len(agents)]
    return agents[0]


async def agent_turn(
    request, channel, config: CollabConfig, user, models: dict, agent_id: str, nudge: Optional[str] = None
) -> Optional[str]:
    """Executa el torn d'un agent: crea el missatge placeholder al canal i
    llança el pipeline complet de chat completion cap allà; espera que acabi
    (torns seqüencials per evitar conflictes d'edició al projecte). Retorna el
    contingut final del missatge (per detectar el marcador FEINA_ACABADA)."""
    from open_webui.routers.channels import new_message_handler

    model = models.get(agent_id)
    if not model:
        await post_notice(
            request, channel, user, f"⚠️ L'agent `{agent_id}` ja no està disponible; el salto."
        )
        return None

    if not await _circuit_allows(channel.id, agent_id):
        await post_notice(
            request, channel, user, f"⚡ L'agent `{agent_id}` està temporalment en pausa (circuit obert)."
        )
        return None

    resolved = await _resolved_agent(channel.id, agent_id)
    name = _agent_display_name(resolved, model, agent_id)
    effective_model_id = await _budget_model_or_none(
        request, channel, user, agent_id, "turn", resolved
    )
    if effective_model_id is None:
        return None
    effective_model = models.get(effective_model_id, model)
    degraded = await _is_degraded(channel.id, await _channel_budget(channel.id))
    context_config = config
    if degraded:
        context_config = config.model_copy(
            update={"guardrails": {**config.guardrails, "context_messages": 5}}
        )
    transcript = await build_transcript(channel.id, context_config, models)
    board = await _board_text(channel.id)
    phase = await _current_phase(channel.id, config)
    # Arbre de fitxers precalculat fora del loop (I/O de disc); només si no
    # estem en mode degradat (que l'omet per estalviar tokens).
    tree_text = None
    if config.project_dir and not degraded:
        tree_text = await asyncio.to_thread(project_tree_text, config)

    # Placeholder amb "treballant" perquè els agents lents sense streaming
    # (p.ex. Codex, que ho retorna tot al final) no semblin morts.
    response_message, channel = await new_message_handler(
        request,
        channel.id,
        MessageForm(
            content="⏳ *treballant…*",
            data={},
            meta={"model_id": agent_id, "model_name": name},
        ),
        user,
        None,
    )

    def _compose_messages(transcript_text: str, include_tree: bool) -> list[dict]:
        system = (
            f"Ets {name}, membre d'un EQUIP d'IAs que treballa unit en una taula rodona, "
            f"juntament amb: {_participants_line(config, models, exclude=agent_id)} i les "
            "persones usuàries.\n\n"
            + _PHILOSOPHY
            + "\n\nRegles pràctiques:\n"
            "- Adreça't als altres pel seu nom quan els responguis.\n"
            "- Sigues concret; no repeteixis el que ja s'ha dit ni facis resums de cortesia.\n"
            "- Tauler d'equip (si tens tools): `list_tasks()`, `create_task(title, assignee)`, "
            "`update_task(task_id, status|assignee|notes)`. Manteniu-lo al dia.\n"
            "- Aquí sota veus només els missatges recents, però TOTA la conversa queda "
            "guardada: si necessites revisar tot el que s'ha fet o buscar una decisió "
            "antiga (o l'usuari t'ho demana), usa `read_conversation(offset, limit)` o "
            "`search_conversation(query)`."
            + _phase_block(phase)
            + _project_block(config, include_tree=include_tree, tree_text=tree_text)
            + board
        )
        system = _apply_agent_prompt(system, resolved, name)
        prompt = (
            "Transcripció recent de la taula rodona (autors entre claudàtors):\n\n"
            f"{transcript_text}\n\n"
            f"És el teu torn, {name}. Continua la conversa."
        )
        if nudge:
            prompt += f"\n\n[Avís del sistema: {nudge}]"
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

    messages = _compose_messages(transcript, include_tree=not degraded)
    estimated_input = sum(estimate_tokens(m["content"]) for m in messages)

    turn_id = str(uuid.uuid4())
    form_data = {
        "model": effective_model_id,
        "messages": messages,
        "stream": True,
        "chat_id": f"channel:{channel.id}",
        "id": response_message.id,
        "session_id": f"channel:{channel.id}",
        "background_tasks": {},
        # Les eines llegeixen variables.collab, però els models tipus pipe
        # construeixen __metadata__ des de form_data.metadata. Cal enviar el
        # mateix context pels dos camins perquè Codex/Claude respectin els
        # guardrails del canal (especialment turn_timeout).
        **collab_generation_context(channel, config, turn_id),
    }

    # Gestió de fitxers i tauler EXTERNS als models: eines estàndard
    # (fitxers + tasques + propose_finish) adjuntades a cada torn perquè
    # qualsevol model (Ollama, APIs, pipes...) hi tingui accés. També fem
    # foto de la carpeta per detectar canvis.
    files_before = None
    if (
        not _model_lacks_tools(effective_model_id)
        and await ensure_collab_tool(user.id)
    ):
        form_data["tool_ids"] = [COLLAB_TOOL_ID]
    if resolved.get("tools") is not None:
        allowed_tools = set(resolved["tools"])
        form_data["tool_ids"] = [
            tool_id for tool_id in form_data.get("tool_ids", []) if tool_id in allowed_tools
        ]
    if resolved.get("token_limit"):
        form_data["max_tokens"] = int(resolved["token_limit"])
    if resolved.get("effort") and _model_supports_effort(effective_model):
        form_data["reasoning_effort"] = resolved["effort"]
    if config.project_dir:
        # snapshot() pot fer milers de stat() en carpetes grans: fora del loop.
        files_before = await asyncio.to_thread(snapshot, config.project_dir)

    async def _run_with_backpressure():
        async with acquire_model_slot(effective_model_id):
            content = await _run_generation_until_done(
                request, form_data, user, response_message.id
            )
            retry_delay = _retry_after_seconds(content)
            has_tools = bool(form_data.get("tool_ids"))
            tools_unsupported = bool(
                has_tools and content and _TOOL_CALLING_UNSUPPORTED_RE.search(content)
            )
            # Alguns models (p. ex. Gemini) responen amb una tool-call silenciosa
            # i deixen el text final BUIT quan se'ls adjunten eines i la tasca no
            # en necessita cap (saludar, opinar...). Un torn buit amb eines es
            # reintenta un cop SENSE eines abans de donar-lo per caigut.
            empty_with_tools = has_tools and not (content or "").strip()
            if retry_delay is None and not tools_unsupported and not empty_with_tools:
                return content

            retry_status, retry_detail = classify_error(content)
            await _record_usage_safely(
                channel.id,
                agent_id,
                "turn",
                model_id=effective_model_id,
                input_tokens=estimated_input,
                output_tokens=estimate_tokens(content),
                status=retry_status,
                error_detail=retry_detail,
            )
            await _record_circuit_result(channel.id, agent_id, retry_status)

            retry_form = dict(form_data)
            if tools_unsupported:
                _models_without_collab_tools[effective_model_id] = time.time()
                retry_form.pop("tool_ids", None)
                log.warning(
                    "El model %s no admet tool calling; es repeteix el torn sense eines",
                    effective_model_id,
                )
            elif empty_with_tools:
                # No el memoritzem com a "sense eines" (pot funcionar en un torn
                # que sí requereixi fitxers); només aquest torn va sense eines.
                retry_form.pop("tool_ids", None)
                await _reset_response_for_retry(response_message.id)
                log.warning(
                    "Torn buit amb eines de %s; es repeteix sense eines", effective_model_id
                )
                return await _run_generation_until_done(
                    request, retry_form, user, response_message.id
                )
            else:
                # Si el límit és de TOKENS (TPM, prompt massa gran...), repetir
                # la mateixa petició tornaria a petar: es reconstrueix el prompt
                # en mode lleuger (5 missatges de context, sense arbre) perquè
                # càpiga dins la finestra del proveïdor.
                if _TOKEN_LIMIT_HINT_RE.search(content or ""):
                    try:
                        lean_config = config.model_copy(
                            update={"guardrails": {**config.guardrails, "context_messages": 5}}
                        )
                        lean_transcript = await build_transcript(channel.id, lean_config, models)
                        retry_form["messages"] = _compose_messages(
                            lean_transcript, include_tree=False
                        )
                        log.warning(
                            "Reintent en mode lleuger (menys context) per límit de tokens de %s",
                            effective_model_id,
                        )
                    except Exception:
                        log.exception("No s'ha pogut construir el prompt lleuger; reintent normal")
                log.warning(
                    "Rate-limit transitori de %s; es repeteix el torn en %.2fs",
                    effective_model_id,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)

            await _reset_response_for_retry(response_message.id)
            return await _run_generation_until_done(
                request, retry_form, user, response_message.id
            )

    # W1: estat «speaking» visible en temps real a la barra d'agents.
    await _emit_turn_event(
        channel.id,
        "turn_started",
        agent_id,
        response_message.id,
        {"name": name, "model_id": effective_model_id},
    )

    generation_task = asyncio.create_task(_run_with_backpressure())
    _turn_cancellables[turn_id] = {
        "channel_id": channel.id,
        "agent_id": agent_id,
        "message_id": response_message.id,
        "task": generation_task,
        "started_at": time.time(),
        "cancel_reason": None,
        "cancel_pending": False,
        "tool_lock_depth": 0,
        "active_tool": None,
    }
    final_content: Optional[str] = None
    turn_timed_out = False
    try:
        final_content = await asyncio.wait_for(
            generation_task, timeout=_effective_turn_timeout(config)
        )
    except asyncio.TimeoutError as exc:
        turn_timed_out = True
        _turn_cancellables[turn_id]["cancel_reason"] = "timeout"
        await _mark_cancelled_message(response_message.id, "timeout")
        log.warning("Torn de %s tallat pel timeout efectiu", agent_id)
        await post_notice(
            request, channel, user, f"⏱️ Torn de {name} cancel·lat per timeout."
        )
    except asyncio.CancelledError:
        reason = _turn_cancellables.get(turn_id, {}).get("cancel_reason") or "cancelled"
        await _mark_cancelled_message(response_message.id, reason)
        await post_notice(
            request, channel, user, f"✖ Torn de {name} cancel·lat ({reason})."
        )
        return None
    except Exception as exc:
        log.exception("El torn de %s ha fallat en llançar-se", agent_id)
        status, detail = classify_error(exc)
        await _record_usage_safely(
            channel.id,
            agent_id,
            "turn",
            model_id=effective_model_id,
            input_tokens=estimated_input,
            status=status,
            error_detail=detail,
        )
        await _record_circuit_result(channel.id, agent_id, status)
        await post_notice(
            request,
            channel,
            user,
            f"⚠️ El torn de {name} ha fallat: {exc}. Continuo la ronda.",
        )
        return None
    finally:
        _turn_cancellables.pop(turn_id, None)
        await _emit_turn_event(
            channel.id,
            "turn_finished",
            agent_id,
            response_message.id,
            {"name": name},
        )

    # Detecció de canvis al projecte (externa als models): foto abans/després
    # del torn i avís 🗂️ al canal amb els fitxers tocats.
    if files_before is not None:
        try:
            files_after = await asyncio.to_thread(snapshot, config.project_dir)
            changes = diff_snapshots(files_before, files_after)
            notice = format_changes(name, changes)
            if notice:
                await post_notice(request, channel, user, notice)
        except Exception:
            log.exception("No s'han pogut detectar els canvis del torn de %s", agent_id)

    # Detecció d'agent caigut (quota exhaurida, timeout, error del CLI) i
    # recuperació si el torn ha anat bé.
    content_error = extract_model_error(final_content)
    failure_reason = None
    if final_content is None:
        failure_reason = "el torn no s'ha pogut executar"
    elif not final_content.strip():
        failure_reason = "torn sense cap resposta (possible límit de quota o penjada)"

    if turn_timed_out:
        usage_status, usage_detail = classify_error(asyncio.TimeoutError("turn_timeout"))
    elif final_content is None or not final_content.strip():
        usage_status, usage_detail = classify_error(final_content)
    elif content_error:
        usage_status, usage_detail = classify_error(content_error)
    else:
        usage_status, usage_detail = STATUS_SUCCESS, None

    if content_error:
        failure_reason = {
            "quota_exceeded": "límit de quota o rate-limit confirmat pel model",
            "timeout": "timeout del model",
            "context_too_large": "context massa gran",
            "empty_response": "torn sense resposta",
            "cli_error": "error del CLI",
            "provider_error": "error del proveïdor",
        }.get(usage_status, "error del model")
    await _record_usage_safely(
        channel.id,
        agent_id,
        "turn",
        model_id=effective_model_id,
        input_tokens=estimated_input,
        output_tokens=estimate_tokens(final_content),
        status=usage_status,
        error_detail=usage_detail,
    )

    if failure_reason:
        await _record_circuit_result(channel.id, agent_id, usage_status)
        await _mark_agent_down(request, channel, user, models, agent_id, failure_reason)
    else:
        await _record_circuit_result(channel.id, agent_id, STATUS_SUCCESS)
        await _mark_agent_up(request, channel, user, models, agent_id)

    # Propostes de consens via marcadors de text (funcionen amb tots els
    # models, també els CLI sense tool-calling): PLA_ACORDAT en planificació,
    # FEINA_ACABADA en qualsevol fase.
    if final_content and not await get_end_proposal(channel.id):
        finish_match = _FINISH_MARKER_RE.search(final_content)
        plan_match = _PLAN_MARKER_RE.search(final_content)
        if finish_match:
            await set_end_proposal(
                channel.id,
                name,
                finish_match.group(1).strip() or "(sense resum)",
                kind="finish",
                by_id=agent_id,
            )
        elif plan_match and phase == "planning":
            await set_end_proposal(
                channel.id,
                name,
                plan_match.group(1).strip() or "(sense detall)",
                kind="plan",
                by_id=agent_id,
            )

    return final_content


async def run_round(request, channel, user, *, event_seq: int | None = None):
    """Bucle principal d'una ronda: torns seqüencials fins a silenci, stop o
    guardarail. Recarrega la config a cada volta perquè els canvis en calent
    (/collab guardrails, agents...) tinguin efecte immediat."""
    if channel.id in _active_rounds:
        # Ja hi ha ronda en marxa: el missatge nou entrarà al context del
        # proper hand-raising automàticament.
        return

    lease_owner = f"round-{uuid.uuid4()}"
    if not await acquire_lease(channel.id, lease_owner):
        # Un altre worker ja processa aquest canal. El missatge humà ja ha
        # quedat persistit i el scheduler propietari el podrà recollir.
        return

    state = {"stop": False, "lease_lost": False}
    _active_rounds[channel.id] = state
    lease_task = asyncio.create_task(_renew_round_lease(channel.id, lease_owner, state))
    # Netegem placeholders de torn orfes d'una ronda anterior interrompuda
    # (crash/reinici): la UI mostraria un agent "parlant" per sempre. Acabem
    # d'adquirir el lease, així que no hi ha cap torn viu d'aquest canal.
    with suppress(Exception):
        await cleanup_orphan_turn_messages(channel.id)
    try:
        turns = 0
        quick_calls = 0
        stall_nudges = 0  # empentes anti-silenci consecutives sense voluntaris
        temp_cleanup_done = False  # W4-2: neteja de .collab_write_*.tmp orfes
        started = time.time()
        last_speaker: Optional[str] = None
        roundrobin_queue: Optional[list[str]] = None
        pending_event_seq = event_seq
        # Últim missatge humà vigent (per marcar receipts com a "incorporated"
        # quan un agent efectivament respon incorporant aquest context).
        active_user_seq = event_seq
        scheduler_event_cursor = event_seq or 0
        config = None
        models: dict = {}

        # Neteja de propostes de tancament velles (d'una ronda interrompuda).
        await clear_end_proposal(channel.id)

        while True:
            fresh_channel = await Channels.get_channel_by_id(channel.id)
            if not fresh_channel:
                break
            config = await _effective_collab_config(fresh_channel)
            if not (config.enabled and config.agents):
                break
            if state["stop"]:
                if not state["lease_lost"]:
                    await post_notice(request, channel, user, "⏹️ Equip aturat.")
                break

            # W4-2: una vegada per ronda, neteja els temporals orfes que hagin
            # quedat d'escriptures atòmiques interrompudes (crash a mig torn).
            if config.project_dir and not temp_cleanup_done:
                temp_cleanup_done = True
                try:
                    from open_webui.collab.files import cleanup_temp_files

                    removed = await asyncio.to_thread(cleanup_temp_files, config.project_dir)
                    if removed:
                        log.info(
                            "Netejats %d temporals orfes de %s", removed, config.project_dir
                        )
                except Exception:
                    log.exception("No s'han pogut netejar els temporals de %s", channel.id)

            # Els receipts d'un missatge humà arribat a mitja ronda s'han de
            # resoldre en qualsevol mode (W9); la preempció (reconstruir la cua
            # i reactivar les empentes) és exclusiva del mode continuous.
            latest_user_seq, scheduler_event_cursor = await _latest_user_event_seq(
                channel.id, scheduler_event_cursor
            )
            if latest_user_seq is not None:
                pending_event_seq = latest_user_seq
                active_user_seq = latest_user_seq
                if config.conversation_mode == "continuous":
                    # Una cua round-robin anterior ja no representa el context
                    # vigent; es reconstrueix per al missatge humà nou.
                    roundrobin_queue = None
                    stall_nudges = 0

            max_turns = int(config.guardrail("max_agent_turns") or 0)
            if max_turns and turns >= max_turns:
                await post_notice(
                    request,
                    channel,
                    user,
                    f"⏸️ Sessió pausada: {max_turns} torns seguits d'agents (guardarail "
                    "`max_agent_turns`). Escriu un missatge o `/collab start` per continuar.",
                )
                break

            round_limit = int(config.guardrail("max_round_seconds") or 0)
            if round_limit and (time.time() - started) > round_limit:
                await post_notice(
                    request,
                    channel,
                    user,
                    f"⏱️ Sessió aturada: ha superat el límit de {round_limit}s "
                    "(guardarail `max_round_seconds`).",
                )
                break

            models = await _get_models(request, user)
            nudge: Optional[str] = None

            if config.mode == "roundrobin":
                if roundrobin_queue is None:
                    roundrobin_queue = list(config.agents)
                if not roundrobin_queue:
                    pending_event_seq = None
                    break  # una passada per tots els agents i s'acaba
                speaker = roundrobin_queue.pop(0)
                if speaker in await get_down_agents(channel.id):
                    if pending_event_seq is not None:
                        await _transition_receipt(
                            channel.id, pending_event_seq, speaker, "pass"
                        )
                    continue  # agent caigut: se salta el seu torn
                if pending_event_seq is not None:
                    await _transition_receipt(
                        channel.id, pending_event_seq, speaker, "will_intervene"
                    )
            else:
                volunteers, responded, asked = await handraise(
                    request,
                    channel,
                    config,
                    user,
                    models,
                    last_speaker,
                    pending_event_seq,
                )
                pending_event_seq = None
                quick_calls += asked
                if state["stop"]:
                    continue
                if volunteers:
                    stall_nudges = 0
                    speaker = volunteers[0]
                else:
                    if asked == 0:
                        await post_notice(
                            request,
                            channel,
                            user,
                            "🔻 Tots els agents de la taula estan caiguts. El sistema "
                            "els reintentarà en uns minuts (escriu un missatge llavors), "
                            "o reintenta'ls manualment des del panell 🤝.",
                        )
                        break
                    if responded == 0:
                        await post_notice(
                            request,
                            channel,
                            user,
                            "⚠️ Cap agent ha pogut respondre a la mà alçada (han "
                            "fallat tots). Revisa els logs del backend o la "
                            "configuració dels agents, i torna-ho a provar.",
                        )
                        break
                    # Silenci: un equip de veritat no s'atura si queda feina.
                    # Empenta: torn forçat al següent agent disponible en rotació.
                    phase = await _current_phase(channel.id, config)
                    pending = [
                        t for t in await get_tasks(channel.id) if t.get("status") != "done"
                    ]
                    down_now = await get_down_agents(channel.id)
                    available = [a for a in config.agents if a not in down_now]
                    if not available:
                        break
                    if stall_nudges < 2:
                        stall_nudges += 1
                        speaker = _next_agent(available, last_speaker)
                        if phase == "planning":
                            nudge = (
                                "L'equip ha quedat en silenci però el pla encara no està "
                                "acordat. Fes avançar la planificació (proposa, pregunta, "
                                "reparteix feina) o, si el pla ja està madur, proposa "
                                "`PLA_ACORDAT:` amb el pla."
                            )
                        elif pending:
                            nudge = (
                                "L'equip ha quedat en silenci però queden tasques obertes "
                                "al tauler. Continua la feina (la teva part o el que "
                                "calgui) o coordina amb els altres qui la fa."
                            )
                        else:
                            nudge = (
                                "L'equip ha quedat en silenci. Si tot està fet i revisat, "
                                "proposa `FEINA_ACABADA:` amb el resum final; si no, "
                                "digues què falta i fes-ho avançar. ATENCIÓ: si el que "
                                "espereu és una resposta o validació de l'USUARI, NO "
                                "proposis tancar — respon només `ESPEREM_USUARI:` i el motiu."
                            )
                    else:
                        if config.guardrail("end_on_silence"):
                            await post_notice(
                                request,
                                channel,
                                user,
                                "😴 L'equip queda en repòs: ningú té res més a fer ara "
                                "mateix. Escriu qualsevol missatge per reactivar-lo.",
                            )
                        break

            final_content = await agent_turn(
                request, channel, config, user, models, speaker, nudge=nudge
            )
            last_speaker = speaker
            turns += 1

            # W9: qui ha parlat efectivament ha INCORPORAT el context del darrer
            # missatge humà (l'ha llegit i hi ha respost). Marca el seu receipt.
            if active_user_seq is not None and final_content and final_content.strip():
                with suppress(Exception):
                    await _transition_receipt(
                        channel.id, active_user_seq, speaker, "incorporated"
                    )

            # L'agent declara que l'equip espera l'usuari: repòs net, sense
            # empentes ni tancaments prematurs.
            if final_content and _WAIT_USER_MARKER in final_content:
                await post_notice(
                    request,
                    channel,
                    user,
                    "⏳ L'equip espera la teva resposta per continuar.",
                )
                break

            # Consens explícit: si algú ha proposat donar el PLA per acordat
            # (PLA_ACORDAT) o la feina per acabada (FEINA_ACABADA /
            # propose_finish), la resta de l'equip vota.
            proposal = await get_end_proposal(channel.id)
            if proposal:
                kind = proposal.get("kind", "finish")
                what = "el pla per acordat" if kind == "plan" else "la feina per acabada"
                await post_notice(
                    request,
                    channel,
                    user,
                    f"🗳️ **{proposal.get('by', 'Un agent')}** proposa donar {what}. "
                    "La resta de l'equip vota…",
                )
                consensus, agrees, disagrees = await _vote_on_proposal(
                    request, channel, config, user, models, proposal
                )
                quick_calls += agrees + disagrees
                await clear_end_proposal(channel.id)
                if consensus and kind == "plan":
                    await set_phase(channel.id, "execution")
                    await post_notice(
                        request,
                        channel,
                        user,
                        f"📋 **Pla acordat** ({agrees} a favor, {disagrees} en contra) — "
                        f"comença l'execució. 🔨\n\n**El pla ({proposal.get('by', '')}):**\n"
                        f"{proposal.get('summary', '')}",
                    )
                    # La ronda continua: ara toca executar.
                elif consensus:
                    await set_phase(channel.id, "planning")  # el proper objectiu començarà planificant
                    await post_notice(
                        request,
                        channel,
                        user,
                        f"✅ **Consens: feina acabada** ({agrees} a favor, {disagrees} en contra).\n\n"
                        f"**Resum final ({proposal.get('by', '')}):**\n{proposal.get('summary', '')}",
                    )
                    break
                else:
                    await post_notice(
                        request,
                        channel,
                        user,
                        f"❌ Sense consens per donar {what} ({agrees} a favor, {disagrees} en "
                        "contra). L'equip continua.",
                    )

        # Final de sessió: estadístiques d'ús (Fase 5) i resum incremental (Fase 4).
        if turns > 0 and config is not None:
            elapsed = int(time.time() - started)
            await post_notice(
                request,
                channel,
                user,
                f"📊 Sessió de treball: {turns} torns d'agent · {quick_calls} crides curtes · "
                f"{elapsed // 60}m{elapsed % 60:02d}s",
            )
            if config.guardrail("auto_summary") and models:
                try:
                    await _update_summary(request, channel, config, user, models)
                except Exception:
                    log.exception("No s'ha pogut actualitzar el resum de %s", channel.id)
    except Exception:
        log.exception("La ronda del canal %s ha petat", channel.id)
        # A diferència dels ~10 camins d'error controlats, un crash intern
        # deixava el canal mut sense cap senyal. Avisem (best-effort) perquè
        # l'usuari sàpiga que ha d'escriure per reactivar l'equip.
        with suppress(Exception):
            await post_notice(
                request,
                channel,
                user,
                "💥 La ronda s'ha aturat per un error intern. Escriu un missatge "
                "per reintentar-ho; si persisteix, revisa els logs del backend.",
            )
    finally:
        _active_rounds.pop(channel.id, None)
        # Neteja de l'estat en memòria d'aquest canal perquè els dicts de mòdul
        # no creixin sense fita al llarg de la vida del procés.
        _budget_notices.pop(channel.id, None)
        for key in [k for k in _handraise_failures if k[0] == channel.id]:
            _handraise_failures.pop(key, None)
        lease_task.cancel()
        # Suprimim QUALSEVOL excepció de la tasca de renovació (no només
        # CancelledError): si ha mort amb un error de BD, rellançar-lo aquí
        # saltaria el release_lease de sota i deixaria el canal bloquejat fins
        # que expirés el TTL. CancelledError hereta de BaseException (no
        # d'Exception), així que cal suprimir-la explícitament.
        with suppress(asyncio.CancelledError, Exception):
            await lease_task
        try:
            await release_lease(
                channel.id,
                lease_owner,
                stopped=state["stop"] and not state["lease_lost"],
            )
        except Exception:
            log.exception("No s'ha pogut alliberar el lease del canal %s", channel.id)
