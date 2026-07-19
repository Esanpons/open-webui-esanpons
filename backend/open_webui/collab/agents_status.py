"""Tracking d'agents caiguts i recuperats.

Codi extret d'orchestrator.py (W7 Pas 4).
Nota: post_notice s'importa tardà per evitar cicles.
"""

import re

from open_webui.collab.tasks import clear_down_agent, get_down_agents, set_down_agent

# Els pipes CLI retornen els errors com blocs Markdown explícits. No busquem
# paraules com ``quota`` o ``rate-limit`` a tota la resposta: un agent pot
# mencionar-les mentre explica l'error d'un altre model i això no converteix
# la seva pròpia resposta en una fallada.
_ERROR_CONTENT_RE = re.compile(
    r"^[ \t]*\*\*(?:Claude|Codex)(?: error:| no ha retornat resposta\.)\*\*",
    re.IGNORECASE | re.MULTILINE,
)
_GENERIC_ERROR_CONTENT_RE = re.compile(r"^\s*(?:Error|Exception):\s+\S", re.IGNORECASE)
_RETRY_DOWN_SECONDS = 300  # reintent automàtic d'un agent caigut cada 5 min


def extract_model_error(content: str | None) -> str | None:
    """Extreu un bloc d'error explícit emès per un pipe Claude/Codex.

    El bloc pot arribar després de contingut parcial si el CLI falla mentre
    transmet la resposta. Retornar només el tram d'error conserva el detall
    útil per classificar-lo sense guardar la resposta completa.
    """
    if not content:
        return None
    match = _ERROR_CONTENT_RE.search(content) or _GENERIC_ERROR_CONTENT_RE.search(content)
    return content[match.start() :].strip() if match else None


async def _mark_agent_down(request, channel, user, models: dict, agent_id: str, reason: str):
    from open_webui.collab.orchestrator import post_notice

    name = models.get(agent_id, {}).get("name", agent_id)
    already_down = agent_id in await get_down_agents(channel.id)
    await set_down_agent(channel.id, agent_id, reason)
    if not already_down:
        await post_notice(
            request,
            channel,
            user,
            f"🔻 **{name}** ha caigut ({reason}). L'equip ho ha de tenir en compte i "
            "repartir-se la seva feina pendent. El sistema el reintentarà cada uns "
            "minuts; també el pots reintentar des del panell 🤝 (botó 🔄).",
        )


async def _mark_agent_up(request, channel, user, models: dict, agent_id: str):
    from open_webui.collab.orchestrator import post_notice

    if await clear_down_agent(channel.id, agent_id):
        name = models.get(agent_id, {}).get("name", agent_id)
        await post_notice(
            request, channel, user, f"🟢 **{name}** torna a estar operatiu i es reincorpora a l'equip."
        )
