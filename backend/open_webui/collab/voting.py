"""Votació de consens i resum incremental de la taula rodona.

Codi extret d'orchestrator.py (W7 Pas 5).
"""

from open_webui.collab.config import CollabConfig
from open_webui.collab.context import build_transcript, _board_text, _project_block
from open_webui.collab.prompts import _PHILOSOPHY
from open_webui.collab.tasks import get_summary, set_summary, get_down_agents


async def _vote_on_proposal(
    request, channel, config: CollabConfig, user, models: dict, proposal: dict
) -> tuple[bool, int, int]:
    """Vot de consens sobre una proposta de tancament. Retorna
    (consens, a_favor, en_contra). El proposant no vota; si és l'únic agent,
    consens automàtic. Empat o cap vot vàlid = NO consens (la feina continua)."""
    from open_webui.collab.orchestrator import _quick_completion
    import re

    proposer = proposal.get("by", "")
    summary = proposal.get("summary", "")
    kind = proposal.get("kind", "finish")
    down = await get_down_agents(channel.id)
    voters = [
        a for a in config.agents if models.get(a, {}).get("name", a) != proposer and a not in down
    ]
    if not voters:
        return True, 0, 0

    transcript = await build_transcript(channel.id, config, models)
    board = await _board_text(channel.id)

    _agree_re = re.compile(r'"agree"\s*:\s*(true|false)', re.IGNORECASE)

    async def vote_one(agent_id: str):
        name = models.get(agent_id, {}).get("name", agent_id)
        system = (
            f"Ets {name}, membre d'un equip d'IAs que treballa unit per assolir "
            "l'objectiu comú.\n\n" + _PHILOSOPHY + _project_block(config) + board
        )
        if kind == "plan":
            ask = (
                f"{proposer} proposa donar el PLA de l'equip per ACORDAT:\n{summary}\n\n"
                "És un pla prou clar i complet (què es farà i qui fa què) per començar "
                "a executar? Si falta parlar res important, vota en contra."
            )
        else:
            ask = (
                f"{proposer} proposa donar la feina de l'equip per ACABADA amb aquest resum:\n"
                f"{summary}\n\n"
                "Està realment complet l'objectiu? Sigues MOLT exigent: vota en contra si "
                "falta res, si el resultat és bàsic o mediocre, si milloraria clarament "
                "aprofitant les capacitats d'algun membre (imatges reals, proves, etc.), o "
                "si el pla prometia una validació de l'usuari que encara no ha arribat."
            )
        prompt = (
            "Transcripció recent de la taula rodona:\n\n"
            f"{transcript}\n\n"
            f"{ask} Respon NOMÉS amb aquest JSON:\n"
            '{"agree": true|false, "reason": "màxim una frase"}'
        )
        content = await _quick_completion(request, user, channel, config, agent_id, system, prompt, "vote")
        if content is None:
            return None
        match = _agree_re.search(content)
        return match.group(1).lower() == "true" if match else None

    import asyncio
    votes = await asyncio.gather(*[vote_one(a) for a in voters])
    agrees = sum(1 for v in votes if v is True)
    disagrees = sum(1 for v in votes if v is False)
    return (agrees > disagrees), agrees, disagrees


async def _update_summary(request, channel, config: CollabConfig, user, models: dict):
    """Resum incremental de l'espai (Fase 4): un agent fa de secretari en
    acabar la ronda i el resum es guarda a channel.meta['collab_summary']."""
    from open_webui.collab.orchestrator import _quick_completion

    agent_id = next((a for a in config.agents if a in models), None)
    if not agent_id:
        return
    previous = await get_summary(channel.id)
    transcript = await build_transcript(channel.id, config, models)
    system = (
        "Ets el secretari d'una taula rodona d'IAs. Mantens un resum viu de "
        "l'estat de la feina de l'equip: objectiu, decisions preses, què està "
        "fet i què queda pendent."
    )
    prompt = (
        (f"Resum anterior:\n{previous}\n\n" if previous else "")
        + f"Conversa recent:\n\n{transcript}\n\n"
        "Retorna NOMÉS el resum actualitzat (màxim 250 paraules), sense preàmbuls."
    )
    content = await _quick_completion(request, user, channel, config, agent_id, system, prompt, "summary")
    if content:
        await set_summary(channel.id, content.strip())
