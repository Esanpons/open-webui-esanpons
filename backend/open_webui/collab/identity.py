"""Identitat visual d'agents (W14).

Aquest mòdul gestiona:
- **Paleta de colors accessibles**: 8 colors WCAG AA.
- **Contrast WCAG**: funció per validar que un color té contrast suficient.
- **Fallback d'identitat**: color estable per hash de nom + avatar per inicial.
- **resolve_agent_identity()**: fusiona overrides amb fallbacks.

El disseny detallat està a ``docs/disseny-w13-w14-modes-identitat-visual.md`` §W14.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, asdict, field
from typing import Any, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paleta de colors accessibles (WCAG AA sobre fons fosc i clar)
# ---------------------------------------------------------------------------

# Colors provats amb contrast ≥ 4.5:1 sobre #1e1e2e (fons actual de la barra).
DEFAULT_PALETTE: list[str] = [
    "#60a5fa",  # Blau
    "#34d399",  # Verd
    "#fbbf24",  # Taronja
    "#a78bfa",  # Lila
    "#f472b6",  # Rosa
    "#22d3ee",  # Cyan
    "#818cf8",  # Indigo
    "#f87171",  # Vermell
]

# Assignació de rols típics a colors (suggeriment, no obligatori)
ROLE_COLORS: dict[str, str] = {
    "coordinator": "#818cf8",   # Indigo
    "implementer": "#fbbf24",   # Taronja
    "reviewer": "#f472b6",      # Rosa
    "tester": "#34d399",        # Verd
    "designer": "#a78bfa",      # Lila
    "documenter": "#22d3ee",    # Cyan
    "critic": "#f87171",        # Vermell
    "default": "#60a5fa",       # Blau
}

BG_DARK = "#1e1e2e"
BG_LIGHT = "#f8f8f8"


# ---------------------------------------------------------------------------
# Contrast WCAG 2.1
# ---------------------------------------------------------------------------


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Converteix '#rrggbb' a (r, g, b). Tolera '#rgb' i sense '#'. """
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    if len(h) != 6:
        raise ValueError(f"Color invàlid: {hex_color}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _relative_luminance(r: int, g: int, b: int) -> float:
    """Calcula la luminància relativa segons WCAG 2.1."""
    def _channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    R = _channel(r / 255.0)
    G = _channel(g / 255.0)
    B = _channel(b / 255.0)
    return 0.2126 * R + 0.7152 * G + 0.0722 * B


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """Calcula el ratio de contrast WCAG 2.1 entre dos colors.

    Retorna un float ≥ 1.0. ≥ 4.5 compleix AA per text normal.
    """
    r1, g1, b1 = _hex_to_rgb(fg_hex)
    r2, g2, b2 = _hex_to_rgb(bg_hex)
    l1 = _relative_luminance(r1, g1, b1)
    l2 = _relative_luminance(r2, g2, b2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def has_good_contrast(hex_color: str, bg_hex: str = BG_DARK, min_ratio: float = 4.5) -> bool:
    """Verifica que un color té contrast WCAG AA sobre un fons.

    Si el color no es pot parsejar, retorna False (el fallback s'aplica).
    """
    try:
        return contrast_ratio(hex_color, bg_hex) >= min_ratio
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Fallback d'identitat
# ---------------------------------------------------------------------------


def _hash_name_to_index(name: str, mod: int) -> int:
    """Hash estable d'un nom a un índex [0, mod)."""
    h = hashlib.md5(name.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % mod


def fallback_color(name: str) -> str:
    """Retorna un color estable basat en el hash del nom de l'agent."""
    idx = _hash_name_to_index(name, len(DEFAULT_PALETTE))
    return DEFAULT_PALETTE[idx]


def fallback_avatar(name: str) -> str:
    """Retorna la primera lletra del nom (o '?' si està buit)."""
    if not name or not name.strip():
        return "?"
    return name.strip()[0].upper()


# ---------------------------------------------------------------------------
# Resolució d'identitat
# ---------------------------------------------------------------------------


@dataclass
class AgentIdentity:
    """Identitat visual resolta d'un agent."""

    agent_id: str
    name: str
    color: str
    avatar: str
    role: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_agent_identity(
    agent_id: str,
    name: str,
    overrides: list[dict],
    bg_hex: str = BG_DARK,
) -> AgentIdentity:
    """Resol la identitat visual efectiva d'un agent.

    Fusiona els overrides del perfil amb els fallbacks:
    1. Si l'override té color i té bon contrast → usa'l. Si no → fallback per hash.
    2. Si l'override té avatar → usa'l. Si no → inicial del nom.
    3. Si l'override té rol → usa'l. Si no → None.
    """
    override = next((o for o in overrides if o.get("model_id") == agent_id), None)

    # Color
    color = None
    if override and override.get("color"):
        c = str(override["color"])
        if has_good_contrast(c, bg_hex):
            color = c
    if color is None:
        color = fallback_color(name or agent_id)

    # Avatar
    avatar = None
    if override and override.get("avatar"):
        avatar = str(override["avatar"])
    if avatar is None:
        avatar = fallback_avatar(name or agent_id)

    # Rol
    role = None
    if override and override.get("role"):
        role = str(override["role"])

    return AgentIdentity(
        agent_id=agent_id,
        name=name or agent_id,
        color=color,
        avatar=avatar,
        role=role,
    )


def resolve_channel_identities(
    agents: list[str],
    agent_names: dict[str, str],
    overrides: list[dict],
    bg_hex: str = BG_DARK,
) -> list[AgentIdentity]:
    """Resol la identitat de tots els agents d'un canal.

    Args:
        agents: Llista d'agent_ids.
        agent_names: Map agent_id → nom llegible.
        overrides: Llista d'overrides del perfil/canal.
        bg_hex: Color de fons per validar el contrast.

    Returns:
        Llista d'AgentIdentity, una per agent.
    """
    return [
        resolve_agent_identity(aid, agent_names.get(aid, aid), overrides, bg_hex)
        for aid in agents
    ]
