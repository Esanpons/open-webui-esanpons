"""Modes configurables i presets de conversa (W13).

Aquest mòdul gestiona:
- **PresetDefinition**: dataclass amb els camps de mode d'un preset.
- **PRESETS**: 4 presets predefinits (debate, standup, code_review, quick_help).
- **resolve_preset()**: converteix un preset a ProfileForm per crear-lo com a perfil.

El disseny detallat està a ``docs/disseny-w13-w14-modes-identitat-visual.md`` §W13.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Preset definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PresetDefinition:
    """Definició immutable d'un preset de mode de conversa."""

    key: str
    name: str
    description: str
    mode: str  # "handraise" | "roundrobin"
    conversation_mode: str  # "continuous" | "rounds"
    guardrails: dict[str, Any]
    default_agent_overrides: list[dict]  # overrides suggerits (buit = cap)
    budget: Optional[dict] = None  # budget suggerit (None = il·limitat)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Paleta de presets
# ---------------------------------------------------------------------------

PRESETS: dict[str, PresetDefinition] = {
    "debate": PresetDefinition(
        key="debate",
        name="Debat",
        description="Agents debaten lliurement fins a arribar a consens. Sense límit de torns, context complet.",
        mode="handraise",
        conversation_mode="continuous",
        guardrails={
            "max_agent_turns": 0,  # 0 = il·limitat
            "context_messages": 30,
            "allow_self_reply": True,
        },
        default_agent_overrides=[],
    ),
    "standup": PresetDefinition(
        key="standup",
        name="Standup",
        description="Una passada ordenada per cada agent. Torns curts i estructurats.",
        mode="roundrobin",
        conversation_mode="rounds",
        guardrails={
            "max_agent_turns": 3,
            "context_messages": 15,
            "allow_self_reply": False,
        },
        default_agent_overrides=[],
    ),
    "code_review": PresetDefinition(
        key="code_review",
        name="Revisió de codi",
        description="Revisió llarga i detallada. Hand-raise amb context ampli.",
        mode="handraise",
        conversation_mode="continuous",
        guardrails={
            "max_agent_turns": 20,
            "context_messages": 40,
            "allow_self_reply": True,
            "require_planning": False,
        },
        default_agent_overrides=[],
    ),
    "quick_help": PresetDefinition(
        key="quick_help",
        name="Ajuda ràpida",
        description="Q&A ràpida. Torns limitats, context mínim.",
        mode="handraise",
        conversation_mode="rounds",
        guardrails={
            "max_agent_turns": 5,
            "context_messages": 10,
            "allow_self_reply": False,
            "require_planning": False,
        },
        default_agent_overrides=[],
    ),
}


def list_presets() -> list[dict]:
    """Retorna tots els presets disponibles com a diccionaris."""
    return [p.to_dict() for p in PRESETS.values()]


def get_preset(key: str) -> Optional[PresetDefinition]:
    """Retorna un preset pel seu key. None si no existeix."""
    return PRESETS.get(key)


def preset_to_profile_form(preset: PresetDefinition, user_name: str = "Sistema") -> dict:
    """Converteix un preset a un diccionari compatible amb ProfileForm.

    El mode i el conversation_mode s'emmagatzemen dins ``config`` perquè
    CollabProfile no té columnes separades per aquests camps — viuen al
    JSON ``config`` seguint el mateix patró que CollabConfig.
    """
    return {
        "name": f"[Preset] {preset.name}",
        "description": preset.description,
        "config": {
            "mode": preset.mode,
            "conversation_mode": preset.conversation_mode,
            "guardrails": dict(preset.guardrails),
        },
        "agent_overrides": list(preset.default_agent_overrides),
        "budget": preset.budget,
        "is_template": True,
    }


def extract_mode_from_config(config: dict) -> tuple[str, str]:
    """Extreu el mode i conversation_mode d'un config de perfil/canal.

    Si no hi són, retorna els valors per defecte.
    """
    mode = config.get("mode", "handraise")
    conversation_mode = config.get("conversation_mode", "continuous")
    if mode not in ("handraise", "roundrobin"):
        mode = "handraise"
    if conversation_mode not in ("continuous", "rounds"):
        conversation_mode = "continuous"
    return mode, conversation_mode


def extract_guardrails_from_config(config: dict) -> dict:
    """Extreu els guardrails d'un config de perfil/canal."""
    return config.get("guardrails", {}) or {}
