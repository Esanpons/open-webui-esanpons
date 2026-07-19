"""Context del canal col·laboratiu: transcript, tauler i projecte.

Codi extret d'orchestrator.py (W7 Pas 3).
"""

from typing import Optional

from open_webui.collab.config import CollabConfig
from open_webui.collab.files import tree_as_text
from open_webui.collab.tasks import get_summary, get_tasks, tasks_as_text, get_down_agents
from open_webui.models.channels import ChannelModel
from open_webui.models.messages import Messages
from open_webui.models.users import Users
from open_webui.utils.channels import replace_mentions


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
        # Arbre curt: és una orientació, no un inventari — per a la llista
        # completa els agents tenen list_project_files(). Mantenir-lo petit
        # estalvia tokens a cada torn i evita els límits TPM dels models gratuïts.
        text += (
            "\n\nEstat actual de la carpeta (arbre de fitxers, pot estar tallat):\n"
            + tree_as_text(config.project_dir, max_entries=80)
        )
    return text


def _collab_ctx(channel: ChannelModel, config: CollabConfig) -> dict:
    return {
        "channel_id": channel.id,
        "project_dir": config.project_dir,
        "agents": config.agents,
        # 0 significa explícitament sense timeout. Els pipes CLI respecten
        # aquest mateix valor i l'orquestrador conserva la cancel·lació manual.
        "turn_timeout": int(config.guardrail("turn_timeout") or 0),
    }


def collab_generation_context(
    channel: ChannelModel, config: CollabConfig, turn_id: str
) -> dict:
    """Propaga el mateix context a eines i pipes d'Open WebUI."""
    collab = {**_collab_ctx(channel, config), "turn_id": turn_id}
    return {
        "variables": {"collab": collab},
        "metadata": {"collab": collab},
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
