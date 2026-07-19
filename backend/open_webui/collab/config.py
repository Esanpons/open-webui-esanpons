"""Configuració d'un espai col·laboratiu, desada a channel.meta['collab'].

Els guardarails són 100% configurables per espai (decisió D6 del pla): els
valors d'aquí sota són només suggeriments inicials; cada un es pot ajustar o
desactivar en calent amb `/collab guardrails clau=valor` (0 = desactivat per
als numèrics, on/off per als booleans). Mai apliquem límits fixos al codi.
"""

import logging
import os
from typing import Optional

from open_webui.internal.db import get_async_db_context
from open_webui.models.channels import Channel, ChannelModel
from pydantic import BaseModel, Field
from sqlalchemy import select, update

log = logging.getLogger(__name__)

# Valors inicials suggerits per als guardarails (tots editables per espai).
GUARDRAIL_DEFAULTS = {
    # Torns d'agent seguits sense intervenció humana abans de pausar. 0 = sense
    # límit (per defecte): l'equip treballa en continu fins al consens de
    # FEINA_ACABADA, el repòs per inactivitat o una aturada manual.
    "max_agent_turns": 0,
    # Acabar la ronda quan cap agent vol intervenir (consens implícit).
    "end_on_silence": True,
    # Permetre que un agent respongui immediatament al seu propi missatge.
    "allow_self_reply": True,
    # Segons màxims per torn d'agent (generació completa). 0 = sense timeout.
    "turn_timeout": 900,
    # Segons màxims per resposta de "vols intervenir?". 0 = sense timeout.
    "handraise_timeout": 180,
    # Quants missatges recents del canal es passen com a context a cada agent.
    # Es manté baix a propòsit: el resum incremental (auto_summary) cobreix la
    # part antiga de la conversa amb molts menys tokens que els missatges crus.
    "context_messages": 15,
    # La decisió de mà alçada només necessita el tram més recent. Mantenir-la
    # curta redueix latència i evita TPM/request-too-large en models gratuïts.
    # 0 = reutilitzar tot el context_messages general.
    "handraise_context_messages": 8,
    # Filosofia d'equip: primer planificar junts (fase 📋), i només quan el pla
    # està consensuat (vot PLA_ACORDAT) es passa a executar (fase 🔨).
    # Desactivat = els agents planifiquen i executen lliurement.
    "require_planning": True,
    # En acabar una ronda amb feina feta, generar/actualitzar el resum
    # incremental de l'espai (1 crida curta extra per ronda). Actiu per
    # defecte: permet treballar amb un context de missatges molt més curt
    # (estalvi net de tokens a cada torn de cada agent).
    "auto_summary": True,
    # Segons màxims que pot durar una ronda sencera. 0 = sense límit.
    "max_round_seconds": 0,
}

VALID_MODES = ("handraise", "roundrobin")
VALID_CONVERSATION_MODES = ("rounds", "continuous")


class CollabConfig(BaseModel):
    enabled: bool = False
    # Llista de model_ids participants — pròpia de cada espai/taula (D7).
    agents: list[str] = Field(default_factory=list)
    # Carpeta-projecte: el xat "corre des d'aquí" com Claude Code/Codex al
    # terminal (D5). None = espai sense projecte.
    project_dir: Optional[str] = None
    # handraise: després de cada missatge es pregunta a cada agent si vol
    # intervenir. roundrobin: una passada per tots els agents en ordre.
    mode: str = "handraise"
    # continuous (per defecte): conversa fluida — les entrades humanes
    # s'incorporen al torn següent sense esperar que la ronda acabi.
    # rounds: es tanca la ronda en curs abans d'incorporar el missatge nou.
    conversation_mode: str = "continuous"
    # Només es guarden els overrides; la resta agafa GUARDRAIL_DEFAULTS.
    guardrails: dict = Field(default_factory=dict)

    def guardrail(self, key: str):
        value = self.guardrails.get(key, GUARDRAIL_DEFAULTS.get(key))
        return value

    def context_messages(self, *, handraise: bool = False) -> int:
        """Nombre de missatges recents a passar com a context, amb semàntica
        única per a 0.

        A diferència dels timeouts (on 0 = «sense límit»), aquí 0/absent vol dir
        «fes servir el default» (mai desactivar del tot el context). Abans,
        `int(guardrail("context_messages") or 30)` feia que 0 dupliqués el
        context a 30 — contradient la doc i sorprenent l'usuari.

        Amb ``handraise=True`` retorna el context (més curt) de la mà alçada:
        min(context general, handraise_context_messages), o el general si el de
        mà alçada és 0/absent.
        """
        default = GUARDRAIL_DEFAULTS["context_messages"]
        general = int(self.guardrail("context_messages") or default)
        if not handraise:
            return general
        hr = int(self.guardrail("handraise_context_messages") or 0)
        return min(general, hr) if hr else general

    def summary(self) -> str:
        lines = [
            f"**Estat:** {'✅ actiu' if self.enabled else '⏸️ inactiu'}",
            f"**Agents:** {', '.join(self.agents) if self.agents else '(cap)'}",
            f"**Projecte:** `{self.project_dir}`" if self.project_dir else "**Projecte:** (cap carpeta)",
            f"**Mode:** {self.mode}",
            f"**Conversa:** {self.conversation_mode}",
            "**Guardarails** (0/off = desactivat):",
        ]
        for key in GUARDRAIL_DEFAULTS:
            value = self.guardrail(key)
            source = " _(per defecte)_" if key not in self.guardrails else ""
            lines.append(f"- `{key}` = `{value}`{source}")
        return "\n".join(lines)


def get_collab_config(channel: ChannelModel) -> CollabConfig:
    data = (channel.meta or {}).get("collab") or {}
    try:
        return CollabConfig(**data)
    except Exception:
        log.exception("Config collab invàlida al canal %s; s'usa la default", channel.id)
        return CollabConfig()


async def save_collab_config(
    channel_id: str,
    config: CollabConfig,
    *,
    expected_version: int | None = None,
) -> tuple[bool, int]:
    """Desa la config a channel.meta['collab'] amb versionatge optimista.

    Retorna ``(ok, new_version)``.
    Si *expected_version* no és ``None`` i no coincideix amb la versió actual,
    retorna ``(False, current_version)`` — el caller ha de rellegir i reintentar.
    """
    async with get_async_db_context() as db:
        result = await db.execute(select(Channel).filter(Channel.id == channel_id))
        channel = result.scalars().first()
        if not channel:
            return False, -1

        current_version = channel.meta_version or 0
        if expected_version is not None and current_version != expected_version:
            return False, current_version  # conflicte detectat

        new_meta = {**(channel.meta or {}), "collab": config.model_dump()}
        new_version = current_version + 1

        update_result = await db.execute(
            update(Channel)
            .where(
                Channel.id == channel_id,
                Channel.meta_version == current_version,
            )
            .values(meta=new_meta, meta_version=new_version)
        )
        if update_result.rowcount == 0:
            await db.rollback()
            result = await db.execute(
                select(Channel.meta_version).filter(Channel.id == channel_id)
            )
            current = result.scalar_one_or_none()
            return False, current if current is not None else -1
        await db.commit()
        return True, new_version


async def get_meta_version(channel_id: str) -> int:
    """Retorna la versió actual de meta d'un canal (per al versionatge optimista)."""
    async with get_async_db_context() as db:
        result = await db.execute(select(Channel.meta_version).filter(Channel.id == channel_id))
        row = result.scalar_one_or_none()
        return row or 0


############################
# Carpetes recents (per al selector de la UI)
############################

RECENT_DIRS_KEY = "collab.recent_dirs"
MAX_RECENT_DIRS = 8


async def get_recent_dirs() -> list[str]:
    from open_webui.models.config import Config as SystemConfig

    dirs = await SystemConfig.get(RECENT_DIRS_KEY, []) or []
    return [d for d in dirs if isinstance(d, str) and os.path.isdir(d)]


async def push_recent_dir(path: str) -> None:
    from open_webui.models.config import Config as SystemConfig

    try:
        current = await SystemConfig.get(RECENT_DIRS_KEY, []) or []
        dirs = [path] + [d for d in current if isinstance(d, str) and d != path]
        await SystemConfig.upsert({RECENT_DIRS_KEY: dirs[:MAX_RECENT_DIRS]})
    except Exception:
        log.exception("No s'ha pogut desar la carpeta recent %s", path)


def admin_only() -> bool:
    """Amb COLLAB_ADMIN_ONLY=true, només els admins poden configurar espais i
    llançar/aturar rondes (els altres usuaris només miren i conversen)."""
    return os.environ.get("COLLAB_ADMIN_ONLY", "").strip().lower() in ("1", "true", "yes")


def allowed_project_roots() -> list[str]:
    """Rutes arrel permeses per a project_dir (env COLLAB_ALLOWED_ROOTS,
    separades per ';'). Buit = qualsevol carpeta existent (només admin)."""
    raw = os.environ.get("COLLAB_ALLOWED_ROOTS", "")
    return [r.strip() for r in raw.split(";") if r.strip()]


def local_mode() -> bool:
    """Mode LOCAL: el backend i l'escriptori són a la mateixa màquina (l'ús
    d'aquest fork). Habilita primitives lligades al host —navegar tot el disc
    sense whitelist, obrir VS Code— que en un desplegament remot serien un risc.

    Actiu si COLLAB_LOCAL_MODE és cert, o —per compatibilitat amb instal·lacions
    existents— si NO hi ha COLLAB_ALLOWED_ROOTS definit (comportament històric:
    sense whitelist s'assumia local). Definir COLLAB_ALLOWED_ROOTS i deixar
    COLLAB_LOCAL_MODE sense activar és la manera d'endurir un desplegament
    compartit.
    """
    raw = os.environ.get("COLLAB_LOCAL_MODE", "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    # Sense valor explícit: local si no hi ha whitelist (compat històric).
    return not allowed_project_roots()


def validate_project_dir(path: str, is_admin: bool) -> tuple[bool, str]:
    """Retorna (ok, motiu). El path ha d'existir i, si hi ha llista blanca,
    penjar d'una de les arrels permeses."""
    normalized = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(normalized):
        return False, f"La carpeta no existeix: `{normalized}`"

    roots = allowed_project_roots()
    if roots:
        for root in roots:
            root_norm = os.path.abspath(os.path.expanduser(root))
            try:
                if os.path.commonpath([root_norm, normalized]) == root_norm:
                    return True, normalized
            except ValueError:
                continue  # unitats diferents a Windows
        return False, (
            "La carpeta no penja de cap ruta permesa (COLLAB_ALLOWED_ROOTS): "
            + ", ".join(f"`{r}`" for r in roots)
        )

    # Sense whitelist, fixar una carpeta arbitrària del host només s'accepta en
    # mode local i per un admin. En un desplegament compartit s'ha d'exigir
    # COLLAB_ALLOWED_ROOTS.
    if not local_mode():
        return False, (
            "Sense COLLAB_ALLOWED_ROOTS definit, triar carpeta només es permet en "
            "mode local (COLLAB_LOCAL_MODE). Defineix una llista d'arrels permeses."
        )
    if not is_admin:
        return False, "Sense COLLAB_ALLOWED_ROOTS definit, només un admin pot triar carpeta."
    return True, normalized
