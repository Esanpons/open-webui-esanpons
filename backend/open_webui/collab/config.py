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
from sqlalchemy import select

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
    "context_messages": 30,
    # Filosofia d'equip: PRIMER planificar junts (fase 📋), i només quan el pla
    # està consensuat (vot PLA_ACORDAT) es passa a executar (fase 🔨).
    # Desactivat = els agents planifiquen i executen lliurement.
    "require_planning": True,
    # En acabar una ronda amb feina feta, generar/actualitzar el resum
    # incremental de l'espai (1 crida curta extra per ronda).
    "auto_summary": False,
    # Segons màxims que pot durar una ronda sencera. 0 = sense límit.
    "max_round_seconds": 0,
}

VALID_MODES = ("handraise", "roundrobin")


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
    # Només es guarden els overrides; la resta agafa GUARDRAIL_DEFAULTS.
    guardrails: dict = Field(default_factory=dict)

    def guardrail(self, key: str):
        value = self.guardrails.get(key, GUARDRAIL_DEFAULTS.get(key))
        return value

    def summary(self) -> str:
        lines = [
            f"**Estat:** {'✅ actiu' if self.enabled else '⏸️ inactiu'}",
            f"**Agents:** {', '.join(self.agents) if self.agents else '(cap)'}",
            f"**Projecte:** `{self.project_dir}`" if self.project_dir else "**Projecte:** (cap carpeta)",
            f"**Mode:** {self.mode}",
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


async def save_collab_config(channel_id: str, config: CollabConfig) -> bool:
    """Desa la config a channel.meta['collab'] sense tocar cap altre camp."""
    async with get_async_db_context() as db:
        result = await db.execute(select(Channel).filter(Channel.id == channel_id))
        channel = result.scalars().first()
        if not channel:
            return False
        channel.meta = {**(channel.meta or {}), "collab": config.model_dump()}
        await db.commit()
        return True


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

    if not is_admin:
        return False, "Sense COLLAB_ALLOWED_ROOTS definit, només un admin pot triar carpeta."
    return True, normalized
