"""API REST de l'espai col·laboratiu (per a la UI del panell).

Muntat a /api/v1/collab (vegeu el registre marcat # [collab-fork] a main.py).
"""

import asyncio
import logging
import os
import shutil
import string
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from open_webui.collab.config import (
    GUARDRAIL_DEFAULTS,
    VALID_MODES,
    admin_only,
    allowed_project_roots,
    get_collab_config,
    get_recent_dirs,
    push_recent_dir,
    save_collab_config,
    validate_project_dir,
)
from open_webui.collab.files import build_tree, list_dirs, read_text_file
from open_webui.collab.orchestrator import is_round_active, request_stop, run_round
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


############################
# Config de l'espai
############################


class CollabConfigForm(BaseModel):
    enabled: Optional[bool] = None
    agents: Optional[list[str]] = None
    project_dir: Optional[str] = None  # "" per treure-la
    mode: Optional[str] = None
    guardrails: Optional[dict] = None
    phase: Optional[str] = None  # "planning" | "execution" (canvi manual de fase)


@router.get("/{channel_id}/config")
async def get_config(request: Request, channel_id: str, user=Depends(get_verified_user)):
    channel = await _get_channel_checked(request, channel_id, user)
    config = get_collab_config(channel)
    return {
        **config.model_dump(),
        "active": is_round_active(channel.id),
        "guardrail_defaults": GUARDRAIL_DEFAULTS,
        "modes": list(VALID_MODES),
        "summary": await get_summary(channel.id),
        "phase": await get_phase(channel.id),
        "can_manage": not admin_only() or user.role == "admin",
        "recent_dirs": await get_recent_dirs(),
        "down_agents": await get_down_agents(channel.id),
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

    if form_data.project_dir is not None:
        if form_data.project_dir.strip() == "":
            config.project_dir = None
        else:
            ok, result = validate_project_dir(form_data.project_dir, is_admin=(user.role == "admin"))
            if not ok:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result)
            config.project_dir = result
            await push_recent_dir(result)

    if form_data.mode is not None:
        if form_data.mode not in VALID_MODES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Mode invàlid: {form_data.mode}")
        config.mode = form_data.mode

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

    await save_collab_config(channel.id, config)
    return {
        **config.model_dump(),
        "active": is_round_active(channel.id),
        "phase": await get_phase(channel.id),
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
    asyncio.create_task(run_round(request, channel, user))
    return {"active": True, "started": True}


@router.post("/{channel_id}/stop")
async def stop_round(request: Request, channel_id: str, user=Depends(get_verified_user)):
    _check_can_manage(user)
    channel = await _get_channel_checked(request, channel_id, user, permission="write")
    stopped = request_stop(channel.id)
    return {"stopped": stopped}


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
    channel = await _get_channel_checked(request, channel_id, user)
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

    if not allowed_project_roots() and user.role != "admin":
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
