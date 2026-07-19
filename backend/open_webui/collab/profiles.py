"""Perfils reutilitzables i personalització d'agents (W11/W12).

Aquest mòdul gestiona:
- **collab_profile**: plantilles reutilitzables (config + agent_overrides + budget).
- **collab_channel_config**: còpia efectiva independent per canal.
- **resolve_agent()**: fusiona la configuració base amb els overrides d'un agent.

El disseny detallat està a ``docs/disseny-w11-w12-perfils.md``.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from open_webui.internal.db import Base, JSONField, get_async_db_context
from open_webui.models.channels import Channel
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, Index, Integer, Text, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models ORM (creats per la migració Alembic corresponent)
# ---------------------------------------------------------------------------


class CollabProfile(Base):
    """Plantilla reutilitzable de configuració col·laborativa."""

    __tablename__ = "collab_profile"
    __table_args__ = (
        Index("idx_collab_profile_user", "user_id"),
    )

    id = Column(Text, primary_key=True)
    user_id = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    config = Column(JSONField, nullable=False, default=dict)
    agent_overrides = Column(JSONField, nullable=False, default=list)
    budget = Column(JSONField, nullable=True)
    is_template = Column(Integer, nullable=False, default=0)  # SQLite BOOLEAN
    updated_at = Column(BigInteger, nullable=False)
    created_at = Column(BigInteger, nullable=False)


class CollabChannelConfig(Base):
    """Configuració efectiva per canal (independent del perfil origen)."""

    __tablename__ = "collab_channel_config"

    channel_id = Column(Text, primary_key=True)
    source_profile_id = Column(Text, nullable=True)
    source_profile_version = Column(BigInteger, nullable=True)
    config = Column(JSONField, nullable=False, default=dict)
    agent_overrides = Column(JSONField, nullable=False, default=list)
    budget = Column(JSONField, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(BigInteger, nullable=False)


# ---------------------------------------------------------------------------
# Pydantic models per validació d'API
# ---------------------------------------------------------------------------


class AgentOverride(BaseModel):
    """Personalització d'un agent individual dins un perfil/config."""

    model_id: str
    display_name: Optional[str] = None
    role: Optional[str] = None
    system_prompt: Optional[str] = None
    effort: Optional[str] = None  # "low" | "medium" | "high"
    token_limit: Optional[int] = None
    tools: Optional[list[str]] = None  # None = totes
    priority: Optional[int] = Field(default=3, ge=1, le=5)
    color: Optional[str] = None
    avatar: Optional[str] = None
    fallback_model_id: Optional[str] = None


class ProfileForm(BaseModel):
    """Formulari per crear/actualitzar un perfil."""

    name: str
    description: Optional[str] = None
    config: dict = Field(default_factory=dict)
    agent_overrides: list[dict] = Field(default_factory=list)
    budget: Optional[dict] = None
    is_template: bool = False


class ChannelConfigForm(BaseModel):
    """Formulari per actualitzar la config efectiva d'un canal."""

    config: Optional[dict] = None
    agent_overrides: Optional[list[dict]] = None
    budget: Optional[dict] = None
    expected_version: Optional[int] = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _insert_for(session, model):
    return pg_insert(model) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(model)


def _validate_overrides(overrides: list[dict], agents: list[str]) -> list[dict]:
    """Filtra els overrides amb model_id que no existeixen a agents.
    Retorna només els vàlids i loga els descartats."""
    valid = []
    for ov in overrides:
        mid = ov.get("model_id")
        if mid and mid in agents:
            valid.append(ov)
        else:
            log.warning("Override ignorat: model_id '%s' no és a agents %s", mid, agents)
    return valid


def sanitize_overrides(overrides: list[dict] | None) -> list[dict]:
    """Normalitza els overrides passant-los per AgentOverride (Pydantic).

    Descarta els camps invàlids (p. ex. effort/priority fora de rang) i les
    entrades sense model_id, en comptes de deixar-los arribar intactes al motor
    i fer-lo fallar contra el proveïdor. Cada override rebutjat es registra.
    """
    if not overrides:
        return []
    clean: list[dict] = []
    for ov in overrides:
        if not isinstance(ov, dict) or not ov.get("model_id"):
            log.warning("Override descartat (sense model_id o no és objecte): %r", ov)
            continue
        try:
            model = AgentOverride(**ov)
        except Exception:
            log.warning("Override invàlid descartat per a model_id=%s: %r",
                        ov.get("model_id"), ov, exc_info=True)
            continue
        # exclude_none: no reescriure els camps que l'usuari no ha tocat.
        clean.append(model.model_dump(exclude_none=True))
    return clean


def sanitize_project_dir(config: dict | None, is_admin: bool) -> dict:
    """Retorna una còpia de ``config`` amb ``project_dir`` revalidat.

    Si la carpeta no passa ``validate_project_dir`` (no existeix o queda fora de
    COLLAB_ALLOWED_ROOTS), s'elimina en comptes d'aplicar-la: aplicar/importar
    un perfil no pot ser una porta del darrere per fixar una carpeta que el
    panell rebutjaria. Retorna la config normalitzada (project_dir absolut si és
    vàlid, o sense project_dir si no ho és).
    """
    from open_webui.collab.config import validate_project_dir

    data = dict(config or {})
    project_dir = (data.get("project_dir") or "").strip()
    if not project_dir:
        data.pop("project_dir", None)
        return data
    ok, result = validate_project_dir(project_dir, is_admin)
    if ok:
        data["project_dir"] = result  # ruta absoluta normalitzada
    else:
        log.warning(
            "project_dir del perfil rebutjat i eliminat en aplicar/importar: %s (%s)",
            project_dir,
            result,
        )
        data.pop("project_dir", None)
    return data


def resolve_agent(agent_id: str, overrides: list[dict]) -> dict:
    """Fusiona els valors base amb l'override de l'agent (si n'hi ha).

    Retorna un diccionari pla amb tots els camps d'AgentOverride resolts.
    """
    base: dict[str, Any] = {
        "model_id": agent_id,
        "display_name": None,
        "role": None,
        "system_prompt": None,
        "effort": None,
        "token_limit": None,
        "tools": None,
        "priority": 3,
        "color": None,
        "avatar": None,
        "fallback_model_id": None,
    }
    override = next((o for o in overrides if o.get("model_id") == agent_id), None)
    if override:
        for k, v in override.items():
            if v is not None and k in base:
                base[k] = v
    return base


def _serialize_profile(p: CollabProfile) -> dict:
    return {
        "id": p.id,
        "user_id": p.user_id,
        "name": p.name,
        "description": p.description,
        "config": p.config or {},
        "agent_overrides": p.agent_overrides or [],
        "budget": p.budget,
        "is_template": bool(p.is_template),
        "updated_at": p.updated_at,
        "created_at": p.created_at,
    }


def _serialize_channel_config(c: CollabChannelConfig) -> dict:
    return {
        "channel_id": c.channel_id,
        "source_profile_id": c.source_profile_id,
        "source_profile_version": c.source_profile_version,
        "config": c.config or {},
        "agent_overrides": c.agent_overrides or [],
        "budget": c.budget,
        "version": c.version,
        "updated_at": c.updated_at,
    }


# ---------------------------------------------------------------------------
# CRUD: collab_profile
# ---------------------------------------------------------------------------


async def list_profiles(user_id: str) -> list[dict]:
    """Llista exclusivament les plantilles creades per l'usuari."""
    async with get_async_db_context() as db:
        result = await db.execute(
            select(CollabProfile)
            .where(CollabProfile.user_id == user_id)
            .order_by(CollabProfile.updated_at.desc())
        )
        return [_serialize_profile(p) for p in result.scalars().all()]


async def get_profile(profile_id: str, user_id: str) -> Optional[dict]:
    """Retorna una plantilla si pertany a l'usuari."""
    async with get_async_db_context() as db:
        result = await db.execute(
            select(CollabProfile).where(
                CollabProfile.id == profile_id,
                CollabProfile.user_id == user_id,
            )
        )
        p = result.scalar_one_or_none()
        return _serialize_profile(p) if p else None


async def create_profile(user_id: str, form: ProfileForm) -> dict:
    now = int(time.time())
    profile = CollabProfile(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name=form.name,
        description=form.description,
        config=form.config,
        agent_overrides=form.agent_overrides,
        budget=form.budget,
        is_template=1,
        updated_at=now,
        created_at=now,
    )
    async with get_async_db_context() as db:
        db.add(profile)
        await db.commit()
    return _serialize_profile(profile)


async def update_profile(profile_id: str, user_id: str, form: ProfileForm) -> Optional[dict]:
    now = int(time.time())
    async with get_async_db_context() as db:
        result = await db.execute(
            select(CollabProfile).where(
                CollabProfile.id == profile_id,
                CollabProfile.user_id == user_id,
            )
        )
        p = result.scalar_one_or_none()
        if not p:
            return None
        p.name = form.name
        p.description = form.description
        p.config = form.config
        p.agent_overrides = form.agent_overrides
        p.budget = form.budget
        p.is_template = 1
        p.updated_at = now
        # Els canals encara vinculats segueixen la plantilla. La configuració
        # efectiva i channel.meta.collab s'actualitzen en la mateixa transacció.
        # Les tasques de la plantilla no es propaguen als canals vinculats en
        # editar-la (només un APPLY explícit substitueix el tauler d'un canal).
        space_config, _template_tasks = _split_template_config(form.config)
        linked_result = await db.execute(
            select(CollabChannelConfig).where(
                CollabChannelConfig.source_profile_id == profile_id
            )
        )
        for linked in linked_result.scalars().all():
            linked.config = space_config
            linked.agent_overrides = form.agent_overrides
            linked.budget = form.budget
            linked.source_profile_version = now
            linked.version += 1
            linked.updated_at = now
            await _sync_channel_meta(db, linked.channel_id, space_config)
        await db.commit()
        return _serialize_profile(p)


async def delete_profile(profile_id: str, user_id: str) -> bool:
    async with get_async_db_context() as db:
        result = await db.execute(
            select(CollabProfile).where(
                CollabProfile.id == profile_id,
                CollabProfile.user_id == user_id,
            )
        )
        p = result.scalar_one_or_none()
        if not p:
            return False
        linked_result = await db.execute(
            select(CollabChannelConfig).where(
                CollabChannelConfig.source_profile_id == profile_id
            )
        )
        for linked in linked_result.scalars().all():
            linked.source_profile_id = None
            linked.source_profile_version = None
            linked.version += 1
        await db.delete(p)
        await db.commit()
        return True


async def duplicate_profile(profile_id: str, user_id: str, new_name: str) -> Optional[dict]:
    now = int(time.time())
    async with get_async_db_context() as db:
        result = await db.execute(
            select(CollabProfile).where(
                CollabProfile.id == profile_id,
                CollabProfile.user_id == user_id,
            )
        )
        original = result.scalar_one_or_none()
        if not original:
            return None
        clone = CollabProfile(
            id=str(uuid.uuid4()),
            user_id=user_id,
            name=new_name,
            description=original.description,
            config=original.config,
            agent_overrides=original.agent_overrides,
            budget=original.budget,
            is_template=1,
            updated_at=now,
            created_at=now,
        )
        db.add(clone)
        await db.commit()
        return _serialize_profile(clone)


def export_profile_json(profile: dict) -> dict:
    """Construeix un JSON autocontingut per exportar."""
    return {
        "format": "collab-profile-v2",
        "name": profile["name"],
        "description": profile.get("description"),
        "config": profile.get("config", {}),
        "agent_overrides": profile.get("agent_overrides", []),
        "budget": profile.get("budget"),
    }


def validate_imported_profile(data: dict) -> tuple[bool, str | None, ProfileForm | None]:
    """Valida l'estructura d'un JSON importat. Retorna (ok, error, form)."""
    if not isinstance(data, dict):
        return False, "El JSON ha de ser un objecte", None
    fmt = data.get("format", "")
    if fmt not in ("collab-profile-v1", "collab-profile-v2"):
        return False, f"Format desconegut: '{fmt}'. Esperat 'collab-profile-v1' o 'collab-profile-v2'", None
    name = data.get("name")
    if not name or not isinstance(name, str):
        return False, "Falta 'name' o no és string", None
    config = data.get("config", {})
    if not isinstance(config, dict):
        return False, "'config' ha de ser un objecte", None
    overrides = data.get("agent_overrides", [])
    if not isinstance(overrides, list):
        return False, "'agent_overrides' ha de ser una llista", None
    budget = data.get("budget")
    if budget is not None and not isinstance(budget, dict):
        return False, "'budget' ha de ser un objecte o null", None
    form = ProfileForm(
        name=name,
        description=data.get("description"),
        config=config,
        agent_overrides=overrides,
        budget=budget,
        is_template=True,
    )
    return True, None, form


# ---------------------------------------------------------------------------
# CRUD: collab_channel_config
# ---------------------------------------------------------------------------


async def get_channel_config(channel_id: str) -> Optional[dict]:
    """Llegeix la configuració efectiva d'un canal (None si no existeix)."""
    async with get_async_db_context() as db:
        result = await db.execute(
            select(CollabChannelConfig).where(CollabChannelConfig.channel_id == channel_id)
        )
        c = result.scalar_one_or_none()
        return _serialize_channel_config(c) if c else None


async def ensure_channel_config(channel_id: str, base_config: dict) -> dict:
    """Lazy migration: si no existeix collab_channel_config pel canal, es crea
    amb la config base i sense overrides. Retorna sempre la config efectiva."""
    existing = await get_channel_config(channel_id)
    if existing:
        return existing
    now = int(time.time())
    c = CollabChannelConfig(
        channel_id=channel_id,
        source_profile_id=None,
        source_profile_version=None,
        config=base_config,
        agent_overrides=[],
        budget=None,
        version=1,
        updated_at=now,
    )
    async with get_async_db_context() as db:
        db.add(c)
        await db.commit()
    return _serialize_channel_config(c)


async def sync_channel_config_from_meta(channel_id: str, base_config: dict) -> dict:
    """Sincronitza la config base del panell preservant overrides i budget.

    Una edició directa del canal el desvincula de la plantilla d'origen, igual
    que ``update_channel_config``. Això evita dues fonts de configuració que
    puguin divergir i aturar silenciosament el motor.
    """
    now = int(time.time())
    async with get_async_db_context() as db:
        result = await db.execute(
            select(CollabChannelConfig).where(
                CollabChannelConfig.channel_id == channel_id
            )
        )
        c = result.scalar_one_or_none()
        if c is None:
            c = CollabChannelConfig(
                channel_id=channel_id,
                source_profile_id=None,
                source_profile_version=None,
                config=base_config,
                agent_overrides=[],
                budget=None,
                version=1,
                updated_at=now,
            )
            db.add(c)
        else:
            c.config = base_config
            c.source_profile_id = None
            c.source_profile_version = None
            c.version += 1
            c.updated_at = now
        await db.commit()
        return _serialize_channel_config(c)


async def _sync_channel_meta(db, channel_id: str, config: dict) -> None:
    """Sincronitza la font canònica que consumeix el motor dins la transacció."""
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if channel is None:
        return
    channel.meta = {**(channel.meta or {}), "collab": dict(config)}
    channel.meta_version = (channel.meta_version or 0) + 1


async def apply_profile(
    channel_id: str, profile_id: str, user_id: str, *, is_admin: bool = False
) -> tuple[bool, Optional[dict]]:
    """Copia un perfil a collab_channel_config. El perfil original queda intacte.
    Retorna (ok, serialized_channel_config).

    ``is_admin`` es fa servir per revalidar ``project_dir``: aplicar un perfil no
    pot fixar una carpeta que ``update_config`` rebutjaria (vegeu
    ``sanitize_project_dir``).
    """
    from open_webui.collab.tasks import replace_tasks

    profile = await get_profile(profile_id, user_id)
    if not profile:
        return False, None

    # La plantilla mana sobre TOT: config d'espai (agents, carpeta, mode,
    # guardrails, enabled) i tauler de tasques (si en porta).
    space_config, template_tasks = _split_template_config(profile["config"])
    # Revalidem la carpeta (no és una porta del darrere) i normalitzem overrides.
    space_config = sanitize_project_dir(space_config, is_admin)
    overrides = sanitize_overrides(profile["agent_overrides"])

    now = int(time.time())
    async with get_async_db_context() as db:
        existing = await db.execute(
            select(CollabChannelConfig).where(
                CollabChannelConfig.channel_id == channel_id
            )
        )
        existing = existing.scalar_one_or_none()

        if existing:
            c = existing
            c.source_profile_id = profile_id
            c.source_profile_version = profile["updated_at"]
            c.config = space_config
            c.agent_overrides = overrides
            c.budget = profile["budget"]
            c.version = existing.version + 1
            c.updated_at = now
        else:
            c = CollabChannelConfig(
                channel_id=channel_id,
                source_profile_id=profile_id,
                source_profile_version=profile["updated_at"],
                config=space_config,
                agent_overrides=overrides,
                budget=profile["budget"],
                version=1,
                updated_at=now,
            )
            db.add(c)
        await _sync_channel_meta(db, channel_id, space_config)
        await db.commit()
        serialized = _serialize_channel_config(c)

    if template_tasks is not None:
        await replace_tasks(channel_id, template_tasks)
    return True, serialized


async def _channel_snapshot(channel_id: str) -> Optional[tuple[dict, list[dict], Optional[dict]]]:
    """Fotografia completa de l'estat actual del canal.

    Captura TOTA la configuració:
    - ``channel.meta.collab`` (agents, carpeta, modes, guardrails, enabled)
    - ``collab_channel_config.agent_overrides`` (display_name, role, color, avatar…)
    - ``collab_channel_config.budget``
    - el tauler de tasques amb el seu estat (dins ``config["tasks"]``)

    Retorna ``(config, agent_overrides, budget)`` o ``None`` si el canal no té
    res a desar. Funciona encara que el canal no tingui
    ``collab_channel_config`` (cas normal quan només s'ha configurat des del
    panell general, que escriu a ``channel.meta.collab``).
    """
    from open_webui.collab.tasks import get_tasks

    cfg = await get_channel_config(channel_id)
    canonical_config: dict = {}
    agent_overrides: list[dict] = []
    budget = None

    # 1. channel.meta.collab és la font canònica del panell general
    async with get_async_db_context() as db:
        result = await db.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
        if channel is not None:
            canonical_config = dict((channel.meta or {}).get("collab") or {})

    # 2. Els overrides i el pressupost venen de collab_channel_config (si existeix)
    if cfg:
        if not canonical_config:
            canonical_config = dict(cfg["config"] or {})
        agent_overrides = cfg.get("agent_overrides") or []
        budget = cfg.get("budget")

    if not canonical_config and not agent_overrides:
        return None

    # 3. El tauler de tasques forma part de la plantilla (viatja dins config
    #    sota la clau "tasks"; s'extreu abans d'escriure a channel.meta.collab).
    canonical_config = dict(canonical_config)
    canonical_config.pop("tasks", None)
    canonical_config["tasks"] = [
        {
            "title": t.get("title"),
            "status": t.get("status"),
            "assignee": t.get("assignee"),
            "notes": t.get("notes"),
            "created_by": t.get("created_by"),
        }
        for t in await get_tasks(channel_id)
    ]
    return canonical_config, agent_overrides, budget


def _split_template_config(config: dict | None) -> tuple[dict, Optional[list[dict]]]:
    """Separa la config d'espai i les tasques d'una config de plantilla.

    Retorna ``(config_sense_tasks, tasks_o_None)``. ``None`` = la plantilla no
    porta tauler (plantilles antigues); llista (pot ser buida) = substituir-lo.
    """
    data = dict(config or {})
    tasks = data.pop("tasks", None)
    return data, tasks if isinstance(tasks, list) else None


async def save_as_profile(
    channel_id: str, name: str, description: str, user_id: str
) -> Optional[dict]:
    """Crea una plantilla NOVA amb la fotografia completa del canal."""
    snapshot = await _channel_snapshot(channel_id)
    if snapshot is None:
        return None
    canonical_config, agent_overrides, budget = snapshot

    now = int(time.time())
    profile = CollabProfile(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name=name,
        description=description,
        config=canonical_config,
        agent_overrides=agent_overrides,
        budget=budget,
        is_template=1,
        updated_at=now,
        created_at=now,
    )
    async with get_async_db_context() as db:
        db.add(profile)
        await db.commit()
    return _serialize_profile(profile)


async def save_into_profile(channel_id: str, profile_id: str, user_id: str) -> Optional[dict]:
    """Desa la fotografia completa del canal DINS una plantilla existent.

    Conserva el nom i la descripció de la plantilla; la resta (config amb
    agents/carpeta/mode/guardrails, personalitzacions i pressupost) se
    sobreescriu amb l'estat actual del canal. Els altres canals vinculats a la
    plantilla s'actualitzen (mateix comportament que editar-la).
    """
    existing = await get_profile(profile_id, user_id)
    if not existing:
        return None
    snapshot = await _channel_snapshot(channel_id)
    if snapshot is None:
        return None
    canonical_config, agent_overrides, budget = snapshot
    return await update_profile(
        profile_id,
        user_id,
        ProfileForm(
            name=existing["name"],
            description=existing.get("description"),
            config=canonical_config,
            agent_overrides=agent_overrides,
            budget=budget,
            is_template=True,
        ),
    )


async def update_channel_config(
    channel_id: str, form: ChannelConfigForm
) -> tuple[bool, Optional[dict]]:
    """Actualitza la config efectiva amb versionatge optimista.
    Retorna (ok, serialized). Si la versió no coincideix → (False, current)."""
    now = int(time.time())
    async with get_async_db_context() as db:
        result = await db.execute(
            select(CollabChannelConfig).where(
                CollabChannelConfig.channel_id == channel_id
            )
        )
        c = result.scalar_one_or_none()
        if not c:
            # No existeix: la creem
            c = CollabChannelConfig(
                channel_id=channel_id,
                source_profile_id=None,
                source_profile_version=None,
                config=form.config or {},
                agent_overrides=form.agent_overrides or [],
                budget=form.budget,
                version=1,
                updated_at=now,
            )
            db.add(c)
            await _sync_channel_meta(db, channel_id, c.config or {})
            await db.commit()
            return True, _serialize_channel_config(c)

        # Versionatge optimista
        if form.expected_version is not None and c.version != form.expected_version:
            return False, _serialize_channel_config(c)

        if form.config is not None:
            c.config = form.config
        if form.agent_overrides is not None:
            c.agent_overrides = form.agent_overrides
        # ``budget: null`` explícit al body elimina el pressupost; absent = no tocar.
        if "budget" in form.model_fields_set:
            c.budget = form.budget
        # Una edició local crea una còpia independent; futurs canvis de la
        # plantilla no poden sobreescriure-la silenciosament.
        c.source_profile_id = None
        c.source_profile_version = None
        c.version += 1
        c.updated_at = now
        await _sync_channel_meta(db, channel_id, c.config or {})
        await db.commit()
        return True, _serialize_channel_config(c)


async def get_channel_overrides(channel_id: str) -> list[dict]:
    """Retorna els agent_overrides efectius d'un canal ([] si no n'hi ha)."""
    cfg = await get_channel_config(channel_id)
    if not cfg:
        return []
    return cfg.get("agent_overrides") or []


async def get_channel_budget(channel_id: str) -> Optional[dict]:
    """Retorna el budget efectiu d'un canal (None = il·limitat)."""
    cfg = await get_channel_config(channel_id)
    if not cfg:
        return None
    return cfg.get("budget")
