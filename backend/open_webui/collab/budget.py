"""Pressupostos actius de l'espai col·laboratiu — W15 Capa 2 + Capa 3.

Aquest mòdul implementa la lògica de comprovació de pressupostos que l'orquestrador
crida abans de cada crida a un model (turn, hand-raise, vot, resum).

Depèn de:
- **W15 Capa 1** (``usage.py``): proporciona els comptadors acumulats a ``collab_budget_tracker``.
- **W11/W12** (``profiles.py``): el camp ``budget`` viu al perfil i es copia a ``collab_channel_config``.

El disseny complet és a ``docs/disseny-w15-capa2-pressupostos.md`` (Capa 2) i
``docs/disseny-w15-capa3-degradacio-context.md`` (Capa 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from open_webui.collab.usage import (
    get_agent_usage,
    get_channel_usage,
)

log = logging.getLogger(__name__)

# Accions possibles quan el pressupost s'exhaureix.
ACTIONS_PAUSE = "pause"
ACTIONS_DOWNGRADE = "downgrade"
ACTIONS_STOP = "stop"
VALID_ACTIONS = (ACTIONS_PAUSE, ACTIONS_DOWNGRADE, ACTIONS_STOP)

DEFAULT_DEGRADATION_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Preus per model (per calcular estimated_cost)
# ---------------------------------------------------------------------------

# Format: model_id_prefix -> {"input_per_1k": float, "output_per_1k": float}
# El prefix coincideix amb el principi del model_id. Cost 0.0 = local/gratuït.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"input_per_1k": 0.0025, "output_per_1k": 0.01},
    "gpt-4o-mini": {"input_per_1k": 0.00015, "output_per_1k": 0.0006},
    "gpt-4-turbo": {"input_per_1k": 0.01, "output_per_1k": 0.03},
    "o1": {"input_per_1k": 0.015, "output_per_1k": 0.06},
    "o3": {"input_per_1k": 0.015, "output_per_1k": 0.06},
    "o4-mini": {"input_per_1k": 0.0011, "output_per_1k": 0.0044},
    # Anthropic
    "claude-sonnet-4": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "claude-opus-4": {"input_per_1k": 0.015, "output_per_1k": 0.075},
    "claude-3-5-sonnet": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "claude-3-5-haiku": {"input_per_1k": 0.0008, "output_per_1k": 0.004},
    "claude-3-opus": {"input_per_1k": 0.015, "output_per_1k": 0.075},
    "claude-3-haiku": {"input_per_1k": 0.00025, "output_per_1k": 0.00125},
    # Google
    "gemini-2.0-flash": {"input_per_1k": 0.0001, "output_per_1k": 0.0004},
    "gemini-1.5-flash": {"input_per_1k": 0.000075, "output_per_1k": 0.0003},
    "gemini-1.5-pro": {"input_per_1k": 0.00125, "output_per_1k": 0.005},
    # DeepSeek
    "deepseek-chat": {"input_per_1k": 0.00014, "output_per_1k": 0.00028},
    "deepseek-reasoner": {"input_per_1k": 0.00055, "output_per_1k": 0.00219},
    # Local (Ollama, models locals) = gratuity
    # No apareixen aquí; estimate_cost retorna 0.0 per a qualsevol model no llistat.
}


def estimate_cost(model_id: str, input_tokens: int | None, output_tokens: int | None) -> float:
    """Estima el cost en USD d'una crida basant-se en els preus per model.

    Els models locals (no llistats a MODEL_PRICING) tenen cost 0.0.
    Fa coincidir per prefix (el model_id més llarg que encaixa guanya).
    """
    if not model_id:
        return 0.0
    # Cerca el prefix més llarg que coincideix (perquè "gpt-4o-mini" guanyei a "gpt-4o").
    best_match = None
    for prefix in MODEL_PRICING:
        if model_id.startswith(prefix):
            if best_match is None or len(prefix) > len(best_match):
                best_match = prefix
    if not best_match:
        return 0.0
    pricing = MODEL_PRICING[best_match]
    in_tok = input_tokens or 0
    out_tok = output_tokens or 0
    return (in_tok / 1000.0) * pricing["input_per_1k"] + (out_tok / 1000.0) * pricing["output_per_1k"]


# ---------------------------------------------------------------------------
# Resultat de la comprovació
# ---------------------------------------------------------------------------


@dataclass
class BudgetDecision:
    """Decisió de pressupost retornada per ``check_budget``.

    - ``allowed``: True si la crida pot procedir, False si està bloquejada.
    - ``degraded``: True si s'ha d'activar la reducció de context (Capa 3).
    - ``reason``: missatge d'avís per l'usuari/agent si allowed=False.
    - ``action``: acció recomanada si exhaurit: "pause" | "downgrade" | "stop" | None.
    """

    allowed: bool
    degraded: bool
    reason: Optional[str]
    action: Optional[str]


# Resposta per defecte: sense pressupost → tot permès, sense degradació.
ALLOWED_NO_BUDGET = BudgetDecision(allowed=True, degraded=False, reason=None, action=None)


# ---------------------------------------------------------------------------
# Consultes agregades
# ---------------------------------------------------------------------------


async def get_session_total_tokens(channel_id: str) -> int:
    """Suma de tokens consumits per tots els agents del canal."""
    channel_usage = await get_channel_usage(channel_id)
    return channel_usage.get("total_tokens", 0)


async def get_session_total_cost(channel_id: str) -> float:
    """Suma de cost acumulat per tots els agents del canal."""
    channel_usage = await get_channel_usage(channel_id)
    return channel_usage.get("total_cost", 0.0)


# ---------------------------------------------------------------------------
# Punt de comprovació principal
# ---------------------------------------------------------------------------


async def check_budget(
    channel_id: str,
    agent_id: str,
    call_type: str,  # "turn" | "handraise" | "vote" | "summary"
    budget: dict | None,
) -> BudgetDecision:
    """Comprova si una crida pot procedir segons el pressupost.

    Retorna BudgetDecision. Si ``budget`` és None o buit, tot és permès.

    Jerarquia de comprovació (la primera condició que es compleix guanya):
    1. Per agent: ``per_agent_tokens``
    2. Global per tokens: ``session_total_tokens``
    3. Global per cost: ``session_total_cost``
    4. Degradació: si l'ús supera degradation_threshold dels límits anteriors.
    """
    if not budget:
        return ALLOWED_NO_BUDGET

    action_on_exhaustion = budget.get("action_on_exhaustion", ACTIONS_PAUSE)
    if action_on_exhaustion not in VALID_ACTIONS:
        action_on_exhaustion = ACTIONS_PAUSE

    threshold = float(budget.get("degradation_threshold", DEFAULT_DEGRADATION_THRESHOLD))

    # Dades O(1): un SELECT del tracker de l'agent + un del canal.
    agent_usage = await get_agent_usage(channel_id, agent_id)
    agent_tokens = agent_usage["consumed_tokens"]

    session_tokens = await get_session_total_tokens(channel_id)
    session_cost = await get_session_total_cost(channel_id)

    # 1. Límit per agent
    per_agent_limit = budget.get("per_agent_tokens")
    if per_agent_limit is not None and agent_tokens >= per_agent_limit:
        return BudgetDecision(
            allowed=False,
            degraded=False,
            reason=(
                f"Pressupost per agent exhaurit "
                f"({agent_tokens:,} / {per_agent_limit:,} tokens)"
            ),
            action=action_on_exhaustion,
        )

    # 2. Límit global per tokens
    session_limit = budget.get("session_total_tokens")
    if session_limit is not None and session_tokens >= session_limit:
        return BudgetDecision(
            allowed=False,
            degraded=False,
            reason=(
                f"Pressupost de sessió exhaurit "
                f"({session_tokens:,} / {session_limit:,} tokens)"
            ),
            action=action_on_exhaustion,
        )

    # 3. Límit global per cost
    cost_limit = budget.get("session_total_cost")
    if cost_limit is not None and session_cost >= cost_limit:
        return BudgetDecision(
            allowed=False,
            degraded=False,
            reason=(
                f"Cost de sessió exhaurit "
                f"(${session_cost:.2f} / ${cost_limit:.2f})"
            ),
            action=action_on_exhaustion,
        )

    # 4. Degradació: si l'ús supera el threshold d'algun límit actiu.
    degraded = False
    if session_limit is not None and session_tokens >= session_limit * threshold:
        degraded = True
    elif per_agent_limit is not None and agent_tokens >= per_agent_limit * threshold:
        degraded = True
    elif cost_limit is not None and session_cost >= cost_limit * threshold:
        degraded = True

    return BudgetDecision(allowed=True, degraded=degraded, reason=None, action=None)


# ---------------------------------------------------------------------------
# W15 Capa 3 — Helper de degradació de context
# ---------------------------------------------------------------------------


async def _is_degraded(channel_id: str, budget: dict | None = None) -> bool:
    """Retorna True si el pressupost està en estat degradat (>80%).

    W15 Capa 3: l'orquestrador crida aquesta funció una vegada per iteració de
    ronda per decidir si ha de reduir el context enviat als agents.

    Si no hi ha pressupost actiu, retorna False (mai es degrada sense budget).

    Paràmetres:
        channel_id: Identificador del canal.
        budget: Opcional — si ja s'ha llegit el budget del canal, es passa
                per evitar una segona consulta.  Si és ``None``, es retorna
                ``False`` (l'orquestrador ha de llegir el budget ell mateix
                i cridar aquesta funció amb el resultat).

    Exemple d'ús (orquestrador)::

        budget = await _channel_budget(channel_id)
        degraded = await _is_degraded(channel_id, budget)
        if degraded:
            transcript_limit = 5  # en lloc de 30
    """
    if not budget:
        return False
    decision = await check_budget(channel_id, "_degradation_check", "any", budget)
    return decision.degraded
