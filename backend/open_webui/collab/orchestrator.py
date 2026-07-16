"""Orquestrador de la taula rodona: rondes, hand-raising i torns d'agent.

Filosofia (vegeu docs/plans/espai-collaboratiu.md): cap agent director. Després
de cada missatge es pregunta a cada agent si vol intervenir (hand-raising) i
parlen per ordre de prioritat, un torn seqüencial cada vegada, fins que ningú
vol afegir res (consens implícit) o un guardarail configurable atura la ronda.
"""

import asyncio
import json
import logging
import re
import time
from typing import Optional

from open_webui.collab.config import CollabConfig, get_collab_config
from open_webui.collab.file_tools import COLLAB_TOOL_ID, ensure_collab_tool
from open_webui.collab.files import (
    diff_snapshots,
    format_changes,
    snapshot,
    tree_as_text,
)
from open_webui.collab.tasks import (
    clear_down_agent,
    clear_end_proposal,
    get_down_agents,
    get_end_proposal,
    get_phase,
    get_summary,
    get_tasks,
    set_down_agent,
    set_end_proposal,
    set_phase,
    set_summary,
    tasks_as_text,
)
from open_webui.models.channels import ChannelModel, Channels
from open_webui.models.messages import MessageForm, Messages
from open_webui.models.users import Users
from open_webui.utils.channels import replace_mentions
from open_webui.utils.models import get_all_models, get_filtered_models

log = logging.getLogger(__name__)

# Estat en memòria de les rondes actives: channel_id -> {"stop": bool}.
# (Un sol worker; si mai es desplega multi-worker caldrà moure-ho a Redis.)
_active_rounds: dict[str, dict] = {}

SYSTEM_AUTHOR = {"model_id": "collab:system", "model_name": "🤝 Taula rodona"}

_HANDRAISE_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
_INTERVENE_RE = re.compile(r'"intervene"\s*:\s*(true|false)', re.IGNORECASE)
_PRIORITY_RE = re.compile(r'"priority"\s*:\s*(\d+)')
_AGREE_RE = re.compile(r'"agree"\s*:\s*(true|false)', re.IGNORECASE)

# Marcadors de text per a les propostes de consens — funcionen amb QUALSEVOL
# model, també els pipes CLI que no fan tool-calling (l'eina propose_finish és
# l'alternativa per als models amb tools natius).
_FINISH_MARKER_RE = re.compile(r"FEINA_ACABADA\s*:?\s*(.*)", re.DOTALL)
_PLAN_MARKER_RE = re.compile(r"PLA_ACORDAT\s*:?\s*(.*)", re.DOTALL)
_WAIT_USER_MARKER = "ESPEREM_USUARI"

# Detecció d'agents caiguts (quota exhaurida, timeouts, errors del CLI).
_ERROR_CONTENT_RE = re.compile(
    r"\*\*(?:Claude|Codex)[^\n]{0,40}error|usage limit|rate.?limit|no ha retornat resposta",
    re.IGNORECASE,
)
_RETRY_DOWN_SECONDS = 300  # reintent automàtic d'un agent caigut cada 5 min
# Errors consecutius de mà alçada abans de declarar l'agent caigut (in-memory).
_handraise_failures: dict[tuple[str, str], int] = {}


async def _mark_agent_down(request, channel, user, models: dict, agent_id: str, reason: str):
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
    if await clear_down_agent(channel.id, agent_id):
        name = models.get(agent_id, {}).get("name", agent_id)
        await post_notice(
            request, channel, user, f"🟢 **{name}** torna a estar operatiu i es reincorpora a l'equip."
        )

# Filosofia de treball de l'equip (vegeu docs/collab-workspace.md § Filosofia):
# sense piràmide, funcions en lloc de rangs, submissió mútua, primer planificar.
_PHILOSOPHY = (
    "Filosofia de l'equip (IMPORTANT, regeix per sobre de tot):\n"
    "- Sou un EQUIP UNIT, no assistents independents. Ningú està per sobre de "
    "ningú: no hi ha caps ni jerarquia. Cadascú té una FUNCIÓ segons les seves "
    "capacitats, no un rang.\n"
    "- Sotmeteu-vos els uns als altres: escolta, demana opinió, accepta "
    "correccions de bon grat. L'acord de l'equip val més que la iniciativa "
    "individual.\n"
    "- PRIMER es planifica EN EQUIP i DESPRÉS s'executa. Mai facis feina que "
    "l'equip no hagi parlat i acordat.\n"
    "- Coordinació: si la teva feina depèn de la d'un altre, ESPERA que estigui "
    "feta (mira el tauler de tasques) i digues que esperes. Quan acabis una "
    "cosa de la qual algú depèn, ANUNCIA-HO clarament perquè pugui continuar.\n"
    "- L'usuari és un membre més de l'equip: si el pla preveu que validi o "
    "decideixi alguna cosa, demaneu-l'hi explícitament i ESPEREU la seva "
    "resposta abans d'executar aquella part. Si l'equip està esperant una "
    "resposta de l'usuari i tu no tens res més a fer, respon NOMÉS amb la "
    "línia `ESPEREM_USUARI:` i el motiu — mai tanquis la feina sense la "
    "validació que el pla prometia.\n"
    "- QUALITAT per sobre de velocitat: treballeu amb l'ambició d'un bon "
    "professional i APROFITEU les capacitats reals de cada membre (si algú "
    "pot generar imatges de debò, no en feu una d'ASCII; si algú pot executar "
    "o provar codi, proveu-lo). No lliureu una versió mediocre per acabar abans."
)


def _phase_block(phase: str) -> str:
    if phase == "planning":
        return (
            "\n\nFASE ACTUAL: 📋 PLANIFICACIÓ — l'equip encara NO executa.\n"
            "- NO toquis fitxers ni facis la feina encara.\n"
            "- El que toca ara: entendre l'objectiu, fer preguntes, proposar "
            "enfocaments, criticar-los amb arguments i consensuar QUÈ es farà i "
            "COM us repartiu la feina (creeu tasques al tauler amb assignat, si "
            "tens eines).\n"
            "- NO proposis el pla al teu primer torn si cap altre membre encara "
            "no ha opinat: primer escolta almenys una altra veu de l'equip.\n"
            "- Quan creguis que el pla està complet i consensuat per tots, acaba "
            "el teu missatge amb una línia que comenci EXACTAMENT per "
            "`PLA_ACORDAT:` seguida del pla resumit (què farà cadascú). La resta "
            "votarà; si hi ha consens, començareu a executar."
        )
    if phase == "execution":
        return (
            "\n\nFASE ACTUAL: 🔨 EXECUCIÓ — el pla ja està acordat; ara es treballa.\n"
            "- Fes LA TEVA part del pla (marca la tasca 🔵 en començar i ✅ en "
            "acabar, si tens eines) i explica què has fet.\n"
            "- Revisa la feina dels altres quan toqui; si veus un problema, "
            "digues-ho amb respecte i arguments.\n"
            "- Si la teva part depèn d'una tasca d'un altre que encara no està "
            "feta, digues que esperes i cedeix el torn.\n"
            "- Quan acabis una cosa de la qual algú depenia, anuncia-ho.\n"
            "- Quan TOT l'objectiu estigui complet i revisat, acaba amb "
            "`FEINA_ACABADA:` i el resum final. La resta votarà."
        )
    # Mode lliure (require_planning desactivat)
    return (
        "\n\nMode lliure: planifiqueu i executeu amb seny, sempre parlant-ho "
        "abans en equip. Quan TOT estigui complet i revisat, acaba amb "
        "`FEINA_ACABADA:` i el resum final."
    )


async def _current_phase(channel_id: str, config: CollabConfig) -> str:
    if not config.guardrail("require_planning"):
        return "free"
    return await get_phase(channel_id)


def is_round_active(channel_id: str) -> bool:
    return channel_id in _active_rounds


def request_stop(channel_id: str) -> bool:
    state = _active_rounds.get(channel_id)
    if state:
        state["stop"] = True
        return True
    return False


async def handle_collab_message(request, channel, message, user) -> bool:
    """Punt d'entrada des del hook de channels.py. Retorna True si el missatge
    l'ha gestionat el mode col·laboratiu (i per tant NO ha de passar pel
    model_response_handler estàndard)."""
    content = (message.content or "").strip()

    if content.lower().startswith("/collab"):
        from open_webui.collab.commands import handle_command

        await handle_command(request, channel, message, user)
        return True

    config = get_collab_config(channel)

    # Auto-activació: si la taula té agents configurats però ningú ha premut
    # "Activa", el PRIMER missatge humà del canal la posa en marxa sol.
    if config.agents and not config.enabled and not (message.meta or {}).get("model_id"):
        from open_webui.collab.config import save_collab_config
        from open_webui.collab.history import count_messages

        if await count_messages(channel.id) <= 2:
            config.enabled = True
            await save_collab_config(channel.id, config)
            await post_notice(
                request,
                channel,
                user,
                "🤝 Espai col·laboratiu activat automàticament — l'equip es posa en marxa.",
            )
            await run_round(request, channel, user)
            return True

    if not (config.enabled and config.agents):
        return False

    if (message.meta or {}).get("model_id"):
        # Missatge generat per un agent: ja el veu la ronda en curs.
        return True

    await run_round(request, channel, user)
    return True


async def post_notice(request, channel, user, content: str):
    """Publica un missatge informatiu de la taula rodona al canal."""
    from open_webui.routers.channels import new_message_handler

    try:
        await new_message_handler(
            request,
            channel.id,
            MessageForm(content=content, meta={**SYSTEM_AUTHOR, "done": True}),
            user,
            None,
        )
    except Exception:
        log.exception("No s'ha pogut publicar l'avís al canal %s", channel.id)


async def _get_models(request, user) -> dict:
    return {
        model["id"]: model
        for model in await get_filtered_models(await get_all_models(request, user=user), user)
    }


async def build_transcript(channel_id: str, config: CollabConfig, models: dict) -> str:
    """Transcripció recent del canal amb autors etiquetats (D4), en ordre
    cronològic. S'ometen les comandes /collab i els placeholders buits de
    torns en curs."""
    limit = int(config.guardrail("context_messages") or 30)
    messages = (await Messages.get_messages_by_channel_id(channel_id, 0, limit))[::-1]

    user_ids = list({m.user_id for m in messages})
    users = {u.id: u for u in await Users.get_users_by_user_ids(user_ids)}

    lines = []
    for m in messages:
        content = (m.content or "").strip()
        meta = m.meta or {}
        if content.lower().startswith("/collab"):
            continue
        if meta.get("model_id") and not meta.get("done"):
            continue  # torn encara en curs (placeholder "treballant")
        if meta.get("model_id") and not content:
            continue  # torn acabat sense text
        if meta.get("model_id"):
            author = meta.get("model_name") or models.get(meta["model_id"], {}).get(
                "name", meta["model_id"]
            )
        else:
            author_user = users.get(m.user_id)
            author = author_user.name if author_user else "Usuari"
        lines.append(f"[{author}]: {replace_mentions(content)}")

    return "\n\n".join(lines)


def _participants_line(config: CollabConfig, models: dict, exclude: Optional[str] = None) -> str:
    names = []
    for agent_id in config.agents:
        if agent_id == exclude:
            continue
        names.append(models.get(agent_id, {}).get("name", agent_id))
    return ", ".join(names) if names else "(cap altre agent)"


def _project_block(config: CollabConfig, include_tree: bool = False) -> str:
    if not config.project_dir:
        return ""
    text = (
        f"\n\nCarpeta del projecte compartit: {config.project_dir}\n"
        "Per treballar-hi tens les eines del sistema (independents del model): "
        "`list_project_files()`, `read_project_file(path)` i "
        "`write_project_file(path, content)`. Els agents amb accés directe al "
        "disc (Claude Code, Codex) també la tenen com a directori de treball. "
        "Els altres agents hi treballen alhora: revisa què han canviat abans de "
        "trepitjar-los la feina."
    )
    if include_tree:
        text += (
            "\n\nEstat actual de la carpeta (arbre de fitxers):\n"
            + tree_as_text(config.project_dir)
        )
    return text


def _collab_ctx(channel: ChannelModel, config: CollabConfig) -> dict:
    return {
        "channel_id": channel.id,
        "project_dir": config.project_dir,
        "agents": config.agents,
    }


async def _board_text(channel_id: str) -> str:
    """Resum incremental + tauler de tasques + agents caiguts, per al context."""
    parts = []
    summary = await get_summary(channel_id)
    if summary:
        parts.append("Resum de la feina fins ara (mantingut pel sistema):\n" + summary)
    tasks = await get_tasks(channel_id)
    if tasks:
        parts.append("Tauler de tasques de l'equip:\n" + tasks_as_text(tasks))
    down = await get_down_agents(channel_id)
    if down:
        fallen = ", ".join(f"{agent_id} ({info.get('reason', 'error')})" for agent_id, info in down.items())
        parts.append(
            f"⚠️ AGENTS CAIGUTS ara mateix: {fallen}. No compteu amb ells fins que "
            "es recuperin: repartiu-vos la seva feina pendent i no els assigneu res de nou."
        )
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


async def _quick_completion(
    request, user, channel, config: CollabConfig, agent_id: str, system: str, prompt: str, task: str
) -> Optional[str]:
    """Crida curta no-streaming a un agent (mà alçada, vot, resum). Retorna el
    contingut de la resposta o None si falla o supera el handraise_timeout."""
    from open_webui.utils.chat import generate_chat_completion

    form_data = {
        "model": agent_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "metadata": {"collab": {**_collab_ctx(channel, config), "task": task}},
    }

    timeout = int(config.guardrail("handraise_timeout") or 0)
    try:
        coroutine = generate_chat_completion(request, form_data, user, bypass_filter=True)
        response = await (asyncio.wait_for(coroutine, timeout) if timeout else coroutine)
        return response["choices"][0]["message"]["content"] or ""
    except asyncio.TimeoutError:
        log.warning("Crida %s de %s ha superat el timeout (%ss)", task, agent_id, timeout)
        return None
    except Exception:
        log.exception("Crida %s de %s ha fallat", task, agent_id)
        return None


async def _handraise_one(
    request, user, channel, config: CollabConfig, models: dict, agent_id: str, transcript: str, board: str, phase: str
) -> tuple[str, Optional[dict]]:
    """Pregunta a un agent si vol intervenir. Retorna (estat, dades):
    ('yes', {'agent','priority','reason'}) | ('no', None) | ('error', None)."""
    model = models.get(agent_id)
    if not model:
        return ("error", None)
    name = model.get("name", agent_id)

    system = (
        f"Ets {name}, membre d'un equip d'IAs que treballa unit en una taula "
        f"rodona amb: {_participants_line(config, models, exclude=agent_id)} i les "
        "persones usuàries.\n\n" + _PHILOSOPHY + _phase_block(phase)
        + _project_block(config) + board
    )
    if phase == "planning":
        question = (
            "Vols intervenir ARA en la PLANIFICACIÓ (pregunta, proposta, objecció, "
            "repartiment de feina, o donar el pla per acordat)?"
        )
    else:
        question = (
            "Vols intervenir ARA (fer la teva part del pla, revisar la feina d'un "
            "altre, anunciar que has acabat, o proposar tancar)? Si estàs ESPERANT "
            "una tasca d'un altre que encara no està feta, NO intervinguis."
        )
    prompt = (
        "Transcripció recent de la taula rodona:\n\n"
        f"{transcript}\n\n"
        f"{question} NO intervinguis per cortesia ni per repetir el que ja "
        "s'ha dit. Respon NOMÉS amb aquest JSON, sense res més:\n"
        '{"intervene": true|false, "priority": 1-5, "reason": "màxim una frase"}'
    )

    content = await _quick_completion(request, user, channel, config, agent_id, system, prompt, "handraise")
    if content is None:
        return ("error", None)

    intervene, priority, reason = None, 3, ""
    for block in _HANDRAISE_JSON_RE.findall(content):
        try:
            data = json.loads(block)
            if "intervene" in data:
                intervene = bool(data.get("intervene"))
                priority = int(data.get("priority") or 3)
                reason = str(data.get("reason") or "")
                break
        except (ValueError, TypeError):
            continue
    if intervene is None:
        match = _INTERVENE_RE.search(content)
        if match:
            intervene = match.group(1).lower() == "true"
            priority_match = _PRIORITY_RE.search(content)
            priority = int(priority_match.group(1)) if priority_match else 3
    if not intervene:
        return ("no", None)
    return ("yes", {"agent": agent_id, "priority": max(1, min(5, priority)), "reason": reason})


async def handraise(
    request, channel, config: CollabConfig, user, models: dict, last_speaker: Optional[str]
) -> tuple[list[str], int, int]:
    """Ronda de mà alçada. Retorna (voluntaris per ordre de prioritat,
    quants han pogut respondre, quants s'han consultat) — així es distingeix
    el consens ("ningú vol parlar") d'una fallada de tots els agents."""
    transcript = await build_transcript(channel.id, config, models)
    board = await _board_text(channel.id)
    phase = await _current_phase(channel.id, config)

    # Agents caiguts: se salten, excepte quan els toca el reintent automàtic.
    down = await get_down_agents(channel.id)
    now = time.time()
    candidates = []
    for agent_id in config.agents:
        info = down.get(agent_id)
        if info and (now - info.get("since", 0)) < _RETRY_DOWN_SECONDS:
            continue
        candidates.append(agent_id)
    if not config.guardrail("allow_self_reply") and last_speaker in candidates and len(candidates) > 1:
        candidates.remove(last_speaker)

    results = await asyncio.gather(
        *[
            _handraise_one(request, user, channel, config, models, agent_id, transcript, board, phase)
            for agent_id in candidates
        ]
    )

    # Comptabilitat de caiguts: 2 errors seguits de mà alçada → caigut; una
    # resposta vàlida → recuperat.
    for agent_id, (status, _payload) in zip(candidates, results):
        key = (channel.id, agent_id)
        if status == "error":
            _handraise_failures[key] = _handraise_failures.get(key, 0) + 1
            if agent_id in down:
                await set_down_agent(channel.id, agent_id, down[agent_id].get("reason", "error"))
            elif _handraise_failures[key] >= 2:
                await _mark_agent_down(
                    request, channel, user, models, agent_id, "no respon (error o possible límit de quota)"
                )
        else:
            _handraise_failures.pop(key, None)
            if agent_id in down:
                await _mark_agent_up(request, channel, user, models, agent_id)

    volunteers = [payload for status, payload in results if status == "yes"]
    responded = sum(1 for status, _payload in results if status != "error")
    order = {agent_id: idx for idx, agent_id in enumerate(config.agents)}
    volunteers.sort(key=lambda v: (-v["priority"], order.get(v["agent"], 99)))
    return [v["agent"] for v in volunteers], responded, len(candidates)


async def _vote_on_proposal(
    request, channel, config: CollabConfig, user, models: dict, proposal: dict
) -> tuple[bool, int, int]:
    """Vot de consens sobre una proposta de tancament. Retorna
    (consens, a_favor, en_contra). El proposant no vota; si és l'únic agent,
    consens automàtic. Empat o cap vot vàlid = NO consens (la feina continua)."""
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

    async def vote_one(agent_id: str) -> Optional[bool]:
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
        match = _AGREE_RE.search(content)
        return match.group(1).lower() == "true" if match else None

    votes = await asyncio.gather(*[vote_one(a) for a in voters])
    agrees = sum(1 for v in votes if v is True)
    disagrees = sum(1 for v in votes if v is False)
    return (agrees > disagrees), agrees, disagrees


async def _update_summary(request, channel, config: CollabConfig, user, models: dict):
    """Resum incremental de l'espai (Fase 4): un agent fa de secretari en
    acabar la ronda i el resum es guarda a channel.meta['collab_summary']."""
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


def _next_agent(agents: list[str], last_speaker: Optional[str]) -> str:
    """Següent agent en rotació (per a les empentes anti-silenci)."""
    if last_speaker in agents and len(agents) > 1:
        return agents[(agents.index(last_speaker) + 1) % len(agents)]
    return agents[0]


async def agent_turn(
    request, channel, config: CollabConfig, user, models: dict, agent_id: str, nudge: Optional[str] = None
) -> Optional[str]:
    """Executa el torn d'un agent: crea el missatge placeholder al canal i
    llança el pipeline complet de chat completion cap allà; espera que acabi
    (torns seqüencials per evitar conflictes d'edició al projecte). Retorna el
    contingut final del missatge (per detectar el marcador FEINA_ACABADA)."""
    from open_webui.routers.channels import new_message_handler

    model = models.get(agent_id)
    if not model:
        await post_notice(
            request, channel, user, f"⚠️ L'agent `{agent_id}` ja no està disponible; el salto."
        )
        return None

    name = model.get("name", agent_id)
    transcript = await build_transcript(channel.id, config, models)
    board = await _board_text(channel.id)
    phase = await _current_phase(channel.id, config)

    # Placeholder amb "treballant" perquè els agents lents sense streaming
    # (p.ex. Codex, que ho retorna tot al final) no semblin morts.
    response_message, channel = await new_message_handler(
        request,
        channel.id,
        MessageForm(
            content="⏳ *treballant…*",
            data={},
            meta={"model_id": agent_id, "model_name": name},
        ),
        user,
        None,
    )

    system = (
        f"Ets {name}, membre d'un EQUIP d'IAs que treballa unit en una taula rodona, "
        f"juntament amb: {_participants_line(config, models, exclude=agent_id)} i les "
        "persones usuàries.\n\n"
        + _PHILOSOPHY
        + "\n\nRegles pràctiques:\n"
        "- Adreça't als altres pel seu nom quan els responguis.\n"
        "- Sigues concret; no repeteixis el que ja s'ha dit ni facis resums de cortesia.\n"
        "- Tauler d'equip (si tens tools): `list_tasks()`, `create_task(title, assignee)`, "
        "`update_task(task_id, status|assignee|notes)`. Manteniu-lo al dia.\n"
        "- Aquí sota veus només els missatges recents, però TOTA la conversa queda "
        "guardada: si necessites revisar tot el que s'ha fet o buscar una decisió "
        "antiga (o l'usuari t'ho demana), usa `read_conversation(offset, limit)` o "
        "`search_conversation(query)`."
        + _phase_block(phase)
        + _project_block(config, include_tree=True)
        + board
    )
    prompt = (
        "Transcripció recent de la taula rodona (autors entre claudàtors):\n\n"
        f"{transcript}\n\n"
        f"És el teu torn, {name}. Continua la conversa."
    )
    if nudge:
        prompt += f"\n\n[Avís del sistema: {nudge}]"

    form_data = {
        "model": agent_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "chat_id": f"channel:{channel.id}",
        "id": response_message.id,
        "session_id": f"channel:{channel.id}",
        "background_tasks": {},
        # Arriba a les eines i als pipes com a __metadata__['variables']['collab'].
        "variables": {"collab": _collab_ctx(channel, config)},
    }

    # Gestió de fitxers i tauler EXTERNS als models: eines estàndard
    # (fitxers + tasques + propose_finish) adjuntades a cada torn perquè
    # qualsevol model (Ollama, APIs, pipes...) hi tingui accés. També fem
    # foto de la carpeta per detectar canvis.
    files_before = None
    if await ensure_collab_tool(user.id):
        form_data["tool_ids"] = [COLLAB_TOOL_ID]
    if config.project_dir:
        files_before = snapshot(config.project_dir)

    try:
        await request.app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)
    except Exception:
        log.exception("El torn de %s ha fallat en llançar-se", agent_id)
        await post_notice(
            request,
            channel,
            user,
            f"⚠️ El torn de {name} ha fallat en llançar-se; continuo la ronda. (Detall als logs.)",
        )
        return None

    timeout = int(config.guardrail("turn_timeout") or 0)
    start = time.time()
    final_content: Optional[str] = None
    while True:
        message = await Messages.get_message_by_id(response_message.id)
        if not message or (message.meta or {}).get("done"):
            final_content = message.content if message else None
            break
        if timeout and (time.time() - start) > timeout:
            log.warning("Torn de %s tallat per turn_timeout (%ss)", agent_id, timeout)
            await post_notice(
                request,
                channel,
                user,
                f"⚠️ El torn de {name} ha superat el `turn_timeout` ({timeout}s); continuo la ronda.",
            )
            final_content = message.content if message else None
            break
        await asyncio.sleep(1.5)

    # Detecció de canvis al projecte (externa als models): foto abans/després
    # del torn i avís 🗂️ al canal amb els fitxers tocats.
    if files_before is not None:
        try:
            changes = diff_snapshots(files_before, snapshot(config.project_dir))
            notice = format_changes(name, changes)
            if notice:
                await post_notice(request, channel, user, notice)
        except Exception:
            log.exception("No s'han pogut detectar els canvis del torn de %s", agent_id)

    # Detecció d'agent caigut (quota exhaurida, timeout, error del CLI) i
    # recuperació si el torn ha anat bé.
    failure_reason = None
    if final_content is None:
        failure_reason = "el torn no s'ha pogut executar"
    elif not final_content.strip():
        failure_reason = "torn sense cap resposta (possible límit de quota o penjada)"
    elif _ERROR_CONTENT_RE.search(final_content):
        failure_reason = "error del model (possible límit de quota)"

    if failure_reason:
        await _mark_agent_down(request, channel, user, models, agent_id, failure_reason)
    else:
        await _mark_agent_up(request, channel, user, models, agent_id)

    # Propostes de consens via marcadors de text (funcionen amb tots els
    # models, també els CLI sense tool-calling): PLA_ACORDAT en planificació,
    # FEINA_ACABADA en qualsevol fase.
    if final_content and not await get_end_proposal(channel.id):
        finish_match = _FINISH_MARKER_RE.search(final_content)
        plan_match = _PLAN_MARKER_RE.search(final_content)
        if finish_match:
            await set_end_proposal(
                channel.id, name, finish_match.group(1).strip() or "(sense resum)", kind="finish"
            )
        elif plan_match and phase == "planning":
            await set_end_proposal(
                channel.id, name, plan_match.group(1).strip() or "(sense detall)", kind="plan"
            )

    return final_content


async def run_round(request, channel, user):
    """Bucle principal d'una ronda: torns seqüencials fins a silenci, stop o
    guardarail. Recarrega la config a cada volta perquè els canvis en calent
    (/collab guardrails, agents...) tinguin efecte immediat."""
    if channel.id in _active_rounds:
        # Ja hi ha ronda en marxa: el missatge nou entrarà al context del
        # proper hand-raising automàticament.
        return

    state = {"stop": False}
    _active_rounds[channel.id] = state
    try:
        turns = 0
        quick_calls = 0
        stall_nudges = 0  # empentes anti-silenci consecutives sense voluntaris
        started = time.time()
        last_speaker: Optional[str] = None
        roundrobin_queue: Optional[list[str]] = None
        config = None
        models: dict = {}

        # Neteja de propostes de tancament velles (d'una ronda interrompuda).
        await clear_end_proposal(channel.id)

        while True:
            fresh_channel = await Channels.get_channel_by_id(channel.id)
            if not fresh_channel:
                break
            config = get_collab_config(fresh_channel)
            if not (config.enabled and config.agents):
                break
            if state["stop"]:
                await post_notice(request, channel, user, "⏹️ Equip aturat.")
                break

            max_turns = int(config.guardrail("max_agent_turns") or 0)
            if max_turns and turns >= max_turns:
                await post_notice(
                    request,
                    channel,
                    user,
                    f"⏸️ Sessió pausada: {max_turns} torns seguits d'agents (guardarail "
                    "`max_agent_turns`). Escriu un missatge o `/collab start` per continuar.",
                )
                break

            round_limit = int(config.guardrail("max_round_seconds") or 0)
            if round_limit and (time.time() - started) > round_limit:
                await post_notice(
                    request,
                    channel,
                    user,
                    f"⏱️ Sessió aturada: ha superat el límit de {round_limit}s "
                    "(guardarail `max_round_seconds`).",
                )
                break

            models = await _get_models(request, user)
            nudge: Optional[str] = None

            if config.mode == "roundrobin":
                if roundrobin_queue is None:
                    roundrobin_queue = list(config.agents)
                if not roundrobin_queue:
                    break  # una passada per tots els agents i s'acaba
                speaker = roundrobin_queue.pop(0)
                if speaker in await get_down_agents(channel.id):
                    continue  # agent caigut: se salta el seu torn
            else:
                volunteers, responded, asked = await handraise(
                    request, channel, config, user, models, last_speaker
                )
                quick_calls += asked
                if state["stop"]:
                    continue
                if volunteers:
                    stall_nudges = 0
                    speaker = volunteers[0]
                else:
                    if asked == 0:
                        await post_notice(
                            request,
                            channel,
                            user,
                            "🔻 Tots els agents de la taula estan caiguts. El sistema "
                            "els reintentarà en uns minuts (escriu un missatge llavors), "
                            "o reintenta'ls manualment des del panell 🤝.",
                        )
                        break
                    if responded == 0:
                        await post_notice(
                            request,
                            channel,
                            user,
                            "⚠️ Cap agent ha pogut respondre a la mà alçada (han "
                            "fallat tots). Revisa els logs del backend o la "
                            "configuració dels agents, i torna-ho a provar.",
                        )
                        break
                    # Silenci: un equip de veritat no s'atura si queda feina.
                    # Empenta: torn forçat al següent agent disponible en rotació.
                    phase = await _current_phase(channel.id, config)
                    pending = [
                        t for t in await get_tasks(channel.id) if t.get("status") != "done"
                    ]
                    down_now = await get_down_agents(channel.id)
                    available = [a for a in config.agents if a not in down_now]
                    if not available:
                        break
                    if stall_nudges < 2:
                        stall_nudges += 1
                        speaker = _next_agent(available, last_speaker)
                        if phase == "planning":
                            nudge = (
                                "L'equip ha quedat en silenci però el pla encara no està "
                                "acordat. Fes avançar la planificació (proposa, pregunta, "
                                "reparteix feina) o, si el pla ja està madur, proposa "
                                "`PLA_ACORDAT:` amb el pla."
                            )
                        elif pending:
                            nudge = (
                                "L'equip ha quedat en silenci però queden tasques obertes "
                                "al tauler. Continua la feina (la teva part o el que "
                                "calgui) o coordina amb els altres qui la fa."
                            )
                        else:
                            nudge = (
                                "L'equip ha quedat en silenci. Si tot està fet i revisat, "
                                "proposa `FEINA_ACABADA:` amb el resum final; si no, "
                                "digues què falta i fes-ho avançar. ATENCIÓ: si el que "
                                "espereu és una resposta o validació de l'USUARI, NO "
                                "proposis tancar — respon només `ESPEREM_USUARI:` i el motiu."
                            )
                    else:
                        if config.guardrail("end_on_silence"):
                            await post_notice(
                                request,
                                channel,
                                user,
                                "😴 L'equip queda en repòs: ningú té res més a fer ara "
                                "mateix. Escriu qualsevol missatge per reactivar-lo.",
                            )
                        break

            final_content = await agent_turn(
                request, channel, config, user, models, speaker, nudge=nudge
            )
            last_speaker = speaker
            turns += 1

            # L'agent declara que l'equip espera l'usuari: repòs net, sense
            # empentes ni tancaments prematurs.
            if final_content and _WAIT_USER_MARKER in final_content:
                await post_notice(
                    request,
                    channel,
                    user,
                    "⏳ L'equip espera la teva resposta per continuar.",
                )
                break

            # Consens explícit: si algú ha proposat donar el PLA per acordat
            # (PLA_ACORDAT) o la feina per acabada (FEINA_ACABADA /
            # propose_finish), la resta de l'equip vota.
            proposal = await get_end_proposal(channel.id)
            if proposal:
                kind = proposal.get("kind", "finish")
                what = "el pla per acordat" if kind == "plan" else "la feina per acabada"
                await post_notice(
                    request,
                    channel,
                    user,
                    f"🗳️ **{proposal.get('by', 'Un agent')}** proposa donar {what}. "
                    "La resta de l'equip vota…",
                )
                consensus, agrees, disagrees = await _vote_on_proposal(
                    request, channel, config, user, models, proposal
                )
                quick_calls += agrees + disagrees
                await clear_end_proposal(channel.id)
                if consensus and kind == "plan":
                    await set_phase(channel.id, "execution")
                    await post_notice(
                        request,
                        channel,
                        user,
                        f"📋 **Pla acordat** ({agrees} a favor, {disagrees} en contra) — "
                        f"comença l'execució. 🔨\n\n**El pla ({proposal.get('by', '')}):**\n"
                        f"{proposal.get('summary', '')}",
                    )
                    # La ronda continua: ara toca executar.
                elif consensus:
                    await set_phase(channel.id, "planning")  # el proper objectiu començarà planificant
                    await post_notice(
                        request,
                        channel,
                        user,
                        f"✅ **Consens: feina acabada** ({agrees} a favor, {disagrees} en contra).\n\n"
                        f"**Resum final ({proposal.get('by', '')}):**\n{proposal.get('summary', '')}",
                    )
                    break
                else:
                    await post_notice(
                        request,
                        channel,
                        user,
                        f"❌ Sense consens per donar {what} ({agrees} a favor, {disagrees} en "
                        "contra). L'equip continua.",
                    )

        # Final de sessió: estadístiques d'ús (Fase 5) i resum incremental (Fase 4).
        if turns > 0 and config is not None:
            elapsed = int(time.time() - started)
            await post_notice(
                request,
                channel,
                user,
                f"📊 Sessió de treball: {turns} torns d'agent · {quick_calls} crides curtes · "
                f"{elapsed // 60}m{elapsed % 60:02d}s",
            )
            if config.guardrail("auto_summary") and models:
                try:
                    await _update_summary(request, channel, config, user, models)
                except Exception:
                    log.exception("No s'ha pogut actualitzar el resum de %s", channel.id)
    except Exception:
        log.exception("La ronda del canal %s ha petat", channel.id)
    finally:
        _active_rounds.pop(channel.id, None)
