"""API REST de l'espai col·laboratiu (per a la UI del panell).

Muntat a /api/v1/collab (vegeu el registre marcat # [collab-fork] a main.py).
"""

import asyncio
import json
import logging
import os
import shutil
import string
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from open_webui.collab.config import (
    CollabConfig,
    GUARDRAIL_DEFAULTS,
    VALID_MODES,
    VALID_CONVERSATION_MODES,
    admin_only,
    allowed_project_roots,
    get_collab_config,
    get_recent_dirs,
    local_mode,
    push_recent_dir,
    save_collab_config,
    validate_project_dir,
)
from open_webui.collab.files import build_tree, list_dirs, read_text_file
from open_webui.collab.engine import list_events, list_receipts, receipt_summary
from open_webui.collab.identity import resolve_channel_identities
from open_webui.collab.orchestrator import (
    cancel_turn,
    is_round_active,
    reconcile_channel,
    request_stop,
    run_round,
)
from open_webui.collab.profiles import (
    ChannelConfigForm,
    ProfileForm,
    apply_profile,
    create_profile,
    delete_profile,
    duplicate_profile,
    ensure_channel_config,
    export_profile_json,
    get_channel_config,
    get_channel_overrides,
    get_profile,
    list_profiles,
    save_as_profile,
    sync_channel_config_from_meta,
    update_channel_config,
    update_profile,
    validate_imported_profile,
)
from open_webui.collab.tasks import (
    clear_down_agent,
    create_task,
    delete_task,
    get_down_agents,
    get_phase,
    get_summary,
    get_tasks,
    set_phase,
    update_task,
)
from open_webui.models.channels import Channels
from open_webui.utils.auth import get_verified_user

log = logging.getLogger(__name__)

router = APIRouter()


def _check_can_manage(user):
    """Amb COLLAB_ADMIN_ONLY=true, només els admins configuren espais i
    gestionen rondes/tasques."""
    if admin_only() and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Només un admin pot gestionar espais col·laboratius (COLLAB_ADMIN_ONLY actiu)",
        )


def _require_channel_manager(channel, user):
    """Exigeix gestió EXPLÍCITA (admin o propietari del canal) per a operacions
    sensibles —fixar project_dir, obrir VS Code— amb independència de
    COLLAB_ADMIN_ONLY.

    En un canal públic amb escriptura pública, `channel_has_access(strict=False)`
    concedeix escriptura a qualsevol usuari verificat; això no ha de permetre
    triar una carpeta del host ni arrencar processos. Aquestes operacions
    requereixen ser admin o el propietari (channel.user_id).
    """
    if user.role == "admin":
        return
    if getattr(channel, "user_id", None) and channel.user_id == user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Aquesta operació requereix ser admin o propietari del canal.",
    )


async def _get_channel_checked(request: Request, channel_id: str, user, permission: str = "read"):
    from open_webui.routers.channels import channel_has_access

    channel = await Channels.get_channel_by_id(channel_id)
    if not channel:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canal no trobat")

    if user.role == "admin":
        return channel
    if channel.type in ["group", "dm"]:
        if not await Channels.is_user_channel_member(channel.id, user.id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sense accés al canal")
    elif not await channel_has_access(user.id, channel, permission=permission, strict=False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sense accés al canal")
    return channel


async def _validate_models(request: Request, agent_ids: list[str]) -> list[str]:
    """Valida que els model_ids existeixin als models disponibles (W5.3 / S3).

    Retorna la llista de model_ids invàlids (buida = tots vàlids).
    Contrasta amb ``request.app.state.MODELS`` (la mateixa font que fa servir
    main.py per resoldre els models de xat, pipes incloses). Si encara no està
    poblat (p. ex. startup), no bloqueja (fail-open).
    """
    try:
        models = getattr(request.app.state, "MODELS", None) or {}
        if not models:
            return []
        return [a for a in agent_ids if a not in models]
    except Exception:
        log.warning("No s'han pogut validar els model IDs, saltant validació (fail-open)")
        return []


def _resolve_agent_display_names(
    request: Request,
    agents: list[str],
    overrides: list[dict],
) -> dict[str, str]:
    """Resol el nom visible de cada agent seguint la jerarquia:
    display_name del override > nom del model > agent_id."""
    models = getattr(request.app.state, "MODELS", None) or {}
    names: dict[str, str] = {}
    for agent_id in agents:
        override = next((o for o in overrides if o.get("model_id") == agent_id), None)
        if override and override.get("display_name"):
            names[agent_id] = override["display_name"]
        elif agent_id in models and models[agent_id].get("name"):
            names[agent_id] = models[agent_id]["name"]
        else:
            names[agent_id] = agent_id
    return names


############################
# Perfils reutilitzables (W11/W12)
# Aquestes rutes NO tenen {channel_id} perquè són globals per usuari.
# Han d'anar ABANS de les rutes /{channel_id}/... per evitar conflictes.
############################


@router.get("/profiles")
async def list_user_profiles(user=Depends(get_verified_user)):
    return {"profiles": await list_profiles(user.id)}


@router.post("/profiles")
async def create_user_profile(form_data: ProfileForm, user=Depends(get_verified_user)):
    _check_can_manage(user)
    return {"profile": await create_profile(user.id, form_data)}


@router.get("/profiles/{profile_id}")
async def get_user_profile(profile_id: str, user=Depends(get_verified_user)):
    profile = await get_profile(profile_id, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Perfil no trobat")
    return {"profile": profile}


@router.put("/profiles/{profile_id}")
async def update_user_profile(
    profile_id: str, form_data: ProfileForm, user=Depends(get_verified_user)
):
    _check_can_manage(user)
    profile = await update_profile(profile_id, user.id, form_data)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Perfil no trobat")
    return {"profile": profile}


@router.delete("/profiles/{profile_id}")
async def delete_user_profile(profile_id: str, user=Depends(get_verified_user)):
    _check_can_manage(user)
    if not await delete_profile(profile_id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Perfil no trobat o és un template del sistema (no es pot esborrar)",
        )
    return {"deleted": True}


@router.post("/profiles/{profile_id}/duplicate")
async def duplicate_user_profile(
    profile_id: str,
    new_name: str = "",
    user=Depends(get_verified_user),
):
    _check_can_manage(user)
    name = new_name.strip() or "Còpia de perfil"
    profile = await duplicate_profile(profile_id, user.id, name)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Perfil no trobat")
    return {"profile": profile}


@router.get("/profiles/{profile_id}/export")
async def export_user_profile(profile_id: str, user=Depends(get_verified_user)):
    profile = await get_profile(profile_id, user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Perfil no trobat")
    return export_profile_json(profile)


@router.post("/profiles/import")
async def import_profile(data: dict, user=Depends(get_verified_user)):
    _check_can_manage(user)
    ok, error, form = validate_imported_profile(data)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)
    # Un perfil importat no pot fixar una carpeta que el panell rebutjaria ni
    # portar overrides invàlids: se saneja abans de desar-lo.
    from open_webui.collab.profiles import sanitize_overrides, sanitize_project_dir

    form.config = sanitize_project_dir(form.config, is_admin=(user.role == "admin"))
    form.agent_overrides = sanitize_overrides(form.agent_overrides)
    profile = await create_profile(user.id, form)
    return {"profile": profile}


############################
# Presets de mode (W13)
# Sense {channel_id}: han d'anar ABANS de les rutes /{channel_id}/...
############################


@router.get("/presets")
async def list_collab_presets(user=Depends(get_verified_user)):
    """Llista els modes predefinits (debate, standup, code_review, quick_help)."""
    from open_webui.collab.presets import list_presets

    return {"presets": list_presets()}


@router.post("/{channel_id}/preset/apply")
async def apply_preset_to_channel(
    request: Request,
    channel_id: str,
    preset_key: str,
    user=Depends(get_verified_user),
):
    """Aplica un preset al canal: mode + conversation_mode + guardrails.

    Els guardrails es fusionen sobre els actuals (deep-merge `{**base, **preset}`);
    els agents, la carpeta-projecte i l'estat enabled es conserven.
    """
    from open_webui.collab.presets import get_preset

    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    preset = get_preset(preset_key)
    if not preset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Preset desconegut: {preset_key}"
        )

    config = get_collab_config(channel)
    config.mode = preset.mode
    config.conversation_mode = preset.conversation_mode
    config.guardrails = {**config.guardrails, **preset.guardrails}

    ok, new_version = await save_collab_config(channel.id, config)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La configuració ha canviat mentrestant. Refresca i reintenta.",
        )
    await sync_channel_config_from_meta(channel.id, config.model_dump())
    return {
        **config.model_dump(),
        "applied_preset": preset_key,
        "meta_version": new_version,
    }


############################
# Observabilitat: backpressure (W5.2)
############################


@router.get("/backpressure/stats")
async def get_backpressure_stats(user=Depends(get_verified_user)):
    """Retorna estadístiques dels semàfors de backpressure."""
    from open_webui.collab.backpressure import stats as bp_stats

    return bp_stats()


############################
# Config efectiva de canal: perfils + overrides (W11/W12)
############################


@router.get("/{channel_id}/channel-config")
async def get_effective_channel_config(
    request: Request, channel_id: str, user=Depends(get_verified_user)
):
    channel = await _get_channel_checked(request, channel_id, user)
    config = get_collab_config(channel)
    # Lazy migration: crea collab_channel_config si no existeix
    cfg = await ensure_channel_config(channel_id, config.model_dump())
    return {"channel_config": cfg}


@router.put("/{channel_id}/channel-config")
async def update_effective_channel_config(
    request: Request,
    channel_id: str,
    form_data: ChannelConfigForm,
    user=Depends(get_verified_user),
):
    _check_can_manage(user)
    await _get_channel_checked(request, channel_id, user, permission="write")
    ok, cfg = await update_channel_config(channel_id, form_data)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La configuració efectiva ha canviat mentrestant. Refresca i reintenta.",
        )
    return {"channel_config": cfg}


@router.post("/{channel_id}/profile/apply")
async def apply_profile_to_channel(
    request: Request,
    channel_id: str,
    profile_id: str,
    user=Depends(get_verified_user),
):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    ok, cfg = await apply_profile(
        channel_id, profile_id, user.id, is_admin=(user.role == "admin")
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Perfil no trobat")
    return {"channel_config": cfg, "applied": True}


@router.post("/{channel_id}/profile/reset")
async def reset_channel_to_defaults(
    request: Request,
    channel_id: str,
    user=Depends(get_verified_user),
):
    """«Plantilla predeterminada»: torna l'espai a l'estat intern de fàbrica.

    Una plantilla defineix TOT l'estat de la taula; la predeterminada és la
    pissarra neta: sense agents, sense carpeta, mode i guardrails per defecte,
    sense personalitzacions ni tauler de tasques, i desvinculada de qualsevol
    plantilla d'origen.
    """
    from open_webui.collab.tasks import replace_tasks

    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")

    config = CollabConfig()  # estat de fàbrica: enabled=False, agents=[], etc.

    ok, new_version = await save_collab_config(channel.id, config)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La configuració ha canviat mentrestant. Refresca i reintenta.",
        )
    cfg = await update_channel_config(
        channel.id,
        ChannelConfigForm(config=config.model_dump(), agent_overrides=[], budget=None),
    )
    await replace_tasks(channel.id, [])
    return {"channel_config": cfg[1] if isinstance(cfg, tuple) else cfg, "reset": True}


class SaveAsProfileForm(BaseModel):
    name: str = ""
    description: str = ""
    # Si s'indica, la fotografia del canal es desa DINS aquesta plantilla
    # existent (en lloc de crear-ne una de nova).
    profile_id: Optional[str] = None


@router.post("/{channel_id}/profile/save")
async def save_channel_as_profile(
    request: Request,
    channel_id: str,
    form_data: SaveAsProfileForm,
    user=Depends(get_verified_user),
):
    from open_webui.collab.profiles import save_into_profile

    _check_can_manage(user)
    await _get_channel_checked(request, channel_id, user, permission="write")
    if form_data.profile_id:
        profile = await save_into_profile(channel_id, form_data.profile_id, user.id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Plantilla no trobada o el canal no té res a desar",
            )
        return {"profile": profile}
    if not form_data.name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cal un nom per crear una plantilla nova",
        )
    profile = await save_as_profile(channel_id, form_data.name, form_data.description, user.id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Aquest canal encara no té configuració efectiva (crea-la primer)",
        )
    return {"profile": profile}


############################
# Identitat visual d'agents (W14)
############################


@router.get("/{channel_id}/agents/identity")
async def get_agents_identity(
    request: Request, channel_id: str, user=Depends(get_verified_user)
):
    """Retorna la identitat visual efectiva de cada agent del canal.

    Fusiona els overrides del perfil/canal amb els fallbacks (color per hash
    de nom, avatar per inicial, sense rol).

    El nom visible segueix la jerarquia: display_name del override > nom del
    model > agent_id.
    """
    channel = await _get_channel_checked(request, channel_id, user)
    config = get_collab_config(channel)
    overrides = await get_channel_overrides(channel_id)
    agent_names = _resolve_agent_display_names(request, config.agents, overrides)
    identities = resolve_channel_identities(
        agents=config.agents,
        agent_names=agent_names,
        overrides=overrides,
    )
    return {
        "identities": [ai.to_dict() for ai in identities],
    }


############################
# Circuit breaker i salut d'agents (W5.1)
############################


@router.get("/{channel_id}/agents/circuit")
async def get_agents_circuit_status(
    request: Request, channel_id: str, user=Depends(get_verified_user)
):
    """Retorna l'estat del circuit breaker de tots els agents del canal."""
    from open_webui.collab.circuit_breaker import list_circuits

    channel = await _get_channel_checked(request, channel_id, user)
    config = get_collab_config(channel)
    circuits = await list_circuits(channel_id, config.agents)
    return {
        "circuits": [c.to_dict() for c in circuits],
    }


@router.post("/{channel_id}/agents/{agent_id}/circuit/reset")
async def reset_agent_circuit(
    request: Request, channel_id: str, agent_id: str, user=Depends(get_verified_user)
):
    """Reset manual del circuit breaker d'un agent."""
    from open_webui.collab.circuit_breaker import reset_circuit

    _check_can_manage(user)
    await _get_channel_checked(request, channel_id, user, permission="write")
    await reset_circuit(channel_id, agent_id)
    return {"reset": True, "agent_id": agent_id}


############################
# Pressupost i degradació (W15 Capa 2/3)
############################


@router.get("/{channel_id}/budget/status")
async def get_budget_status(
    request: Request, channel_id: str, user=Depends(get_verified_user)
):
    """Retorna l'estat del pressupost del canal, incloent si està degradat.

    W15 Capa 3: el frontend usa aquest endpoint per mostrar el xip "⚡ Estalvi".
    """
    from open_webui.collab.budget import check_budget, DEFAULT_DEGRADATION_THRESHOLD
    from open_webui.collab.profiles import get_channel_config as get_cfg
    from open_webui.collab.usage import get_channel_usage

    await _get_channel_checked(request, channel_id, user)
    cfg = await get_cfg(channel_id)
    budget = cfg.get("budget") if cfg else None
    usage = await get_channel_usage(channel_id)
    degraded = False
    if budget:
        decision = await check_budget(channel_id, "_status_check", "any", budget)
        degraded = decision.degraded
    return {
        "degraded": degraded,
        "threshold": DEFAULT_DEGRADATION_THRESHOLD,
        "budget": budget,
        "usage": usage,
    }


############################
# Estat persistent / re-sync
############################


@router.get("/{channel_id}/events")
async def get_collab_events(
    request: Request,
    channel_id: str,
    since: int = 0,
    limit: int = 200,
    user=Depends(get_verified_user),
):
    await _get_channel_checked(request, channel_id, user)
    events = await list_events(channel_id, since=max(0, since), limit=limit)
    return {
        "events": [
            {
                "id": event.id,
                "seq": event.seq,
                "type": event.type,
                "agent_id": event.agent_id,
                "message_id": event.message_id,
                "payload": event.payload or {},
                "status": event.status,
                "created_at": event.created_at,
            }
            for event in events
        ]
    }


@router.get("/{channel_id}/receipts/{event_seq}")
async def get_collab_receipts(
    request: Request,
    channel_id: str,
    event_seq: int,
    user=Depends(get_verified_user),
):
    await _get_channel_checked(request, channel_id, user)
    if event_seq < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="event_seq ha de ser positiu",
        )
    receipts = await list_receipts(channel_id, event_seq)
    return {
        "event_seq": event_seq,
        "receipts": [
            {
                "agent_id": receipt.agent_id,
                "state": receipt.state,
                "message_id": receipt.message_id,
                "updated_at": receipt.updated_at,
            }
            for receipt in receipts
        ],
        "summary": await receipt_summary(channel_id, event_seq),
    }


############################
# Config de l'espai
############################


class CollabConfigForm(BaseModel):
    enabled: Optional[bool] = None
    agents: Optional[list[str]] = None
    project_dir: Optional[str] = None  # "" per treure-la
    mode: Optional[str] = None
    conversation_mode: Optional[str] = None
    guardrails: Optional[dict] = None
    phase: Optional[str] = None  # "planning" | "execution" (canvi manual de fase)
    expected_meta_version: Optional[int] = None  # versionatge optimista (W4-6)


@router.get("/{channel_id}/config")
async def get_config(request: Request, channel_id: str, user=Depends(get_verified_user)):
    channel = await _get_channel_checked(request, channel_id, user)
    config = get_collab_config(channel)
    can_manage = not admin_only() or user.role == "admin"
    return {
        **config.model_dump(),
        "active": is_round_active(channel.id),
        "guardrail_defaults": GUARDRAIL_DEFAULTS,
        "modes": list(VALID_MODES),
        "conversation_modes": list(VALID_CONVERSATION_MODES),
        "summary": await get_summary(channel.id),
        "phase": await get_phase(channel.id),
        "can_manage": can_manage,
        # recent_dirs és estat GLOBAL (rutes usades a QUALSEVOL canal): només
        # es filtra a qui pot triar carpeta, per no filtrar rutes del host ni
        # activitat d'altres espais a usuaris de només-lectura.
        "recent_dirs": await get_recent_dirs() if can_manage else [],
        "down_agents": await get_down_agents(channel.id),
        "meta_version": channel.meta_version or 0,  # W4-6: per al versionatge optimista
    }


@router.post("/{channel_id}/config")
async def update_config(
    request: Request, channel_id: str, form_data: CollabConfigForm, user=Depends(get_verified_user)
):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    config = get_collab_config(channel)

    if form_data.agents is not None:
        config.agents = [a for a in form_data.agents if isinstance(a, str) and a]
        # W5.3 (S3): valida que els model_ids existeixin
        invalid = await _validate_models(request, config.agents)
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Models no disponibles: {', '.join(invalid)}",
            )

    if form_data.project_dir is not None:
        if form_data.project_dir.strip() == "":
            config.project_dir = None
        else:
            # Fixar una carpeta del host és una operació sensible: admin o
            # propietari del canal, encara que el canal sigui públic amb escriptura.
            _require_channel_manager(channel, user)
            ok, result = validate_project_dir(form_data.project_dir, is_admin=(user.role == "admin"))
            if not ok:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result)
            config.project_dir = result
            await push_recent_dir(result)

    if form_data.mode is not None:
        if form_data.mode not in VALID_MODES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Mode invàlid: {form_data.mode}")
        config.mode = form_data.mode

    if form_data.conversation_mode is not None:
        if form_data.conversation_mode not in VALID_CONVERSATION_MODES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Mode de conversa invàlid: {form_data.conversation_mode}",
            )
        config.conversation_mode = form_data.conversation_mode

    if form_data.guardrails is not None:
        cleaned = {}
        for key, value in form_data.guardrails.items():
            if key not in GUARDRAIL_DEFAULTS:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Guardarail desconegut: {key}")
            default = GUARDRAIL_DEFAULTS[key]
            try:
                cleaned[key] = bool(value) if isinstance(default, bool) else int(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Valor invàlid per {key}")
        config.guardrails = {**config.guardrails, **cleaned}

    if form_data.phase is not None:
        if form_data.phase not in ("planning", "execution"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Fase invàlida: {form_data.phase}")
        await set_phase(channel.id, form_data.phase)

    if form_data.enabled is not None:
        if form_data.enabled and not config.agents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cal definir els agents participants abans d'activar l'espai",
            )
        config.enabled = form_data.enabled
        if not form_data.enabled:
            request_stop(channel.id)

    # W4-6: versionatge optimista.  Si expected_meta_version no coincideix amb
    # la versió actual, respon 409 Conflict perquè el client rellegeixi i reintenti.
    ok, new_version = await save_collab_config(
        channel.id, config, expected_version=form_data.expected_meta_version
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La configuració ha canviat mentrestant. Refresca i reintenta.",
        )
    await sync_channel_config_from_meta(channel.id, config.model_dump())
    return {
        **config.model_dump(),
        "active": is_round_active(channel.id),
        "phase": await get_phase(channel.id),
        "meta_version": new_version,
    }


############################
# Ronda: start / stop
############################


@router.post("/{channel_id}/start")
async def start_round(request: Request, channel_id: str, user=Depends(get_verified_user)):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    config = get_collab_config(channel)
    if not (config.enabled and config.agents):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="L'espai no està actiu o no té agents configurats",
        )
    if is_round_active(channel.id):
        return {"active": True, "started": False}
    await reconcile_channel(channel.id)
    asyncio.create_task(run_round(request, channel, user))
    return {"active": True, "started": True}


@router.post("/{channel_id}/stop")
async def stop_round(request: Request, channel_id: str, user=Depends(get_verified_user)):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    stopped = request_stop(channel.id)
    return {"stopped": stopped}


@router.post("/{channel_id}/turn/cancel")
async def cancel_channel_turn(
    request: Request, channel_id: str, user=Depends(get_verified_user)
):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    cancelled = await cancel_turn(channel.id, reason="user_requested")
    return {"cancelled": cancelled}


class RetryAgentForm(BaseModel):
    agent_id: str


@router.post("/{channel_id}/agents/retry")
async def retry_agent(
    request: Request, channel_id: str, form_data: RetryAgentForm, user=Depends(get_verified_user)
):
    """Reintent manual d'un agent caigut: neteja l'estat i, si l'espai està
    actiu i no hi ha sessió en marxa, posa l'equip a treballar."""
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    await clear_down_agent(channel.id, form_data.agent_id)

    config = get_collab_config(channel)
    started = False
    if config.enabled and config.agents and not is_round_active(channel.id):
        asyncio.create_task(run_round(request, channel, user))
        started = True
    return {"down_agents": await get_down_agents(channel.id), "started": started}


@router.post("/{channel_id}/open-vscode")
async def open_in_vscode(request: Request, channel_id: str, user=Depends(get_verified_user)):
    """Obre la carpeta-projecte en una NOVA finestra de VS Code (`code -n`).

    Només té sentit en desplegament LOCAL (el backend i el VS Code són a la
    mateixa màquina), que és l'ús d'aquest fork. Requereix el CLI `code` al PATH."""
    _check_can_manage(user)
    # Obrir VS Code arrenca un procés al host: només té sentit (i és segur) en
    # mode local. En un desplegament remot seria una primitiva d'execució de
    # processos exposada per API.
    if not local_mode():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Obrir VS Code només està permès en mode local (COLLAB_LOCAL_MODE).",
        )
    channel = await _get_channel_checked(request, channel_id, user)
    _require_channel_manager(channel, user)
    config = get_collab_config(channel)
    if not config.project_dir or not os.path.isdir(config.project_dir):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aquest espai no té carpeta-projecte")

    code_bin = shutil.which("code") or shutil.which("code.cmd")
    if not code_bin:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="No s'ha trobat el CLI `code` de VS Code al PATH del servidor.",
        )

    try:
        # -n força una finestra NOVA (no reutilitza la que ja tinguis oberta).
        # A Windows `code` és un .cmd; el resolem amb which i l'executem directament.
        subprocess.Popen(
            [code_bin, "-n", config.project_dir],
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        log.exception("No s'ha pogut obrir VS Code")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error obrint VS Code: {e}")

    return {"opened": True, "path": config.project_dir}


############################
# Tauler de tasques
############################


class TaskForm(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    notes: Optional[str] = None


@router.get("/{channel_id}/tasks")
async def list_channel_tasks(request: Request, channel_id: str, user=Depends(get_verified_user)):
    channel = await _get_channel_checked(request, channel_id, user)
    return {"tasks": await get_tasks(channel.id)}


@router.post("/{channel_id}/tasks")
async def create_channel_task(
    request: Request, channel_id: str, form_data: TaskForm, user=Depends(get_verified_user)
):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    if not (form_data.title or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La tasca necessita un títol")
    task = await create_task(
        channel.id, form_data.title, created_by=user.name, assignee=form_data.assignee or ""
    )
    return {"task": task, "tasks": await get_tasks(channel.id)}


@router.post("/{channel_id}/tasks/{task_id}")
async def update_channel_task(
    request: Request, channel_id: str, task_id: str, form_data: TaskForm, user=Depends(get_verified_user)
):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    ok, reason = await update_task(
        channel.id,
        task_id,
        title=form_data.title or "",
        status=form_data.status or "",
        assignee=form_data.assignee or "",
        notes=form_data.notes or "",
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)
    return {"tasks": await get_tasks(channel.id)}


@router.delete("/{channel_id}/tasks/{task_id}")
async def delete_channel_task(
    request: Request, channel_id: str, task_id: str, user=Depends(get_verified_user)
):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    if not await delete_task(channel.id, task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tasca no trobada")
    return {"tasks": await get_tasks(channel.id)}


############################
# Fitxers del projecte
############################


@router.get("/{channel_id}/files")
async def get_files(request: Request, channel_id: str, user=Depends(get_verified_user)):
    channel = await _get_channel_checked(request, channel_id, user)
    config = get_collab_config(channel)
    if not config.project_dir:
        return {"project_dir": None, "entries": [], "truncated": False}
    tree = build_tree(config.project_dir)
    return {"project_dir": config.project_dir, **tree}


@router.get("/{channel_id}/files/content")
async def get_file_content(
    request: Request, channel_id: str, path: str, user=Depends(get_verified_user)
):
    channel = await _get_channel_checked(request, channel_id, user)
    config = get_collab_config(channel)
    if not config.project_dir:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sense carpeta-projecte")
    ok, result = read_text_file(config.project_dir, path)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result)
    return {"path": path, "content": result}


############################
# Selector de carpeta (browse)
############################


def _default_roots() -> list[str]:
    roots = allowed_project_roots()
    if roots:
        return [os.path.abspath(os.path.expanduser(r)) for r in roots]
    if os.name == "nt":
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
    return ["/"]


@router.get("/browse")
async def browse_dirs(path: Optional[str] = None, user=Depends(get_verified_user)):
    """Llista subcarpetes per al selector de carpeta-projecte de la UI."""
    roots = _default_roots()

    # Sense whitelist, navegar exposa TOT el sistema de fitxers del host. Només
    # es permet en mode local (i a un admin); un desplegament compartit ha de
    # definir COLLAB_ALLOWED_ROOTS per acotar-ho.
    if not allowed_project_roots():
        if not local_mode():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Navegar carpetes sense COLLAB_ALLOWED_ROOTS només es permet "
                "en mode local (COLLAB_LOCAL_MODE). Defineix una llista d'arrels permeses.",
            )
        if user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Sense COLLAB_ALLOWED_ROOTS definit, només un admin pot navegar carpetes",
            )

    if not path:
        return {
            "path": None,
            "parent": None,
            "dirs": [{"name": r, "path": r} for r in roots],
            "recent": await get_recent_dirs(),
        }

    normalized = os.path.abspath(os.path.expanduser(path))
    if allowed_project_roots():
        ok, result = validate_project_dir(normalized, is_admin=(user.role == "admin"))
        if not ok:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=result)
        normalized = result
    if not os.path.isdir(normalized):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La carpeta no existeix")

    parent = os.path.dirname(normalized.rstrip("\\/"))
    if parent == normalized or not parent:
        parent = None
    return {"path": normalized, "parent": parent, "dirs": list_dirs(normalized)}
