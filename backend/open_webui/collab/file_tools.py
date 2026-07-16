"""Eina estàndard d'Open WebUI amb les operacions de fitxers del projecte.

Aquesta és la peça que fa la gestió d'arxius EXTERNA als models: es registra
com un Tool normal (taula `tool`, id `collab_files`) i l'orquestrador l'adjunta
a cada torn (`tool_ids`). Així QUALSEVOL model — Ollama, OpenAI/API, pipes —
pot llistar, llegir i escriure fitxers de la carpeta-projecte via el mecanisme
natiu de tool-calling d'Open WebUI, sense dependre de cap client concret.
La carpeta arriba per __metadata__['variables']['collab']['project_dir'].
"""

import logging

from open_webui.models.tools import ToolForm, ToolMeta, Tools
from open_webui.utils.plugin import load_tool_module_by_id
from open_webui.utils.tools import get_tool_specs

log = logging.getLogger(__name__)

COLLAB_TOOL_ID = "collab_files"
COLLAB_TOOL_VERSION = "3"  # puja-ho quan canviï TOOL_CONTENT per forçar re-registre

TOOL_CONTENT = '''"""
title: Espai col·laboratiu (fitxers + tasques)
description: Eines de la taula rodona — fitxers de la carpeta-projecte, tauler de tasques compartit i proposta de tancament. Gestionat pel sistema, independent del model.
version: ''' + COLLAB_TOOL_VERSION + '''
"""

from open_webui.collab.files import (
    MAX_TREE_ENTRIES,
    read_text_file,
    tree_as_text,
    write_text_file,
)
from open_webui.collab import history as collab_history
from open_webui.collab import tasks as collab_tasks


def _collab(__metadata__: dict) -> dict:
    md = __metadata__ or {}
    ctx = md.get("collab") or (md.get("variables") or {}).get("collab") or {}
    return ctx if isinstance(ctx, dict) else {}


def _project_dir(__metadata__: dict) -> str:
    return _collab(__metadata__).get("project_dir") or ""


def _channel_id(__metadata__: dict) -> str:
    return _collab(__metadata__).get("channel_id") or ""


def _agent_name(__metadata__: dict) -> str:
    model = (__metadata__ or {}).get("model") or {}
    return model.get("name") or model.get("id") or "agent"


class Tools:
    def list_project_files(self, __metadata__: dict = {}) -> str:
        """
        Llista l'estructura de fitxers i carpetes del projecte compartit de la taula rodona.
        :return: Arbre de fitxers en text pla.
        """
        project_dir = _project_dir(__metadata__)
        if not project_dir:
            return "Aquest espai no té carpeta-projecte configurada."
        return tree_as_text(project_dir, max_entries=MAX_TREE_ENTRIES)

    def read_project_file(self, path: str, __metadata__: dict = {}) -> str:
        """
        Llegeix el contingut d'un fitxer de text del projecte compartit.
        :param path: Ruta relativa del fitxer dins del projecte (p.ex. "src/app.py").
        :return: Contingut del fitxer, o el motiu de l'error.
        """
        project_dir = _project_dir(__metadata__)
        if not project_dir:
            return "Aquest espai no té carpeta-projecte configurada."
        ok, result = read_text_file(project_dir, path)
        return result if ok else f"ERROR: {result}"

    def write_project_file(self, path: str, content: str, __metadata__: dict = {}) -> str:
        """
        Escriu (crea o sobreescriu completament) un fitxer de text del projecte compartit. Crea les carpetes intermèdies si cal.
        :param path: Ruta relativa del fitxer dins del projecte (p.ex. "src/app.py").
        :param content: Contingut COMPLET que ha de quedar al fitxer.
        :return: Confirmació o motiu de l'error.
        """
        project_dir = _project_dir(__metadata__)
        if not project_dir:
            return "Aquest espai no té carpeta-projecte configurada."
        ok, result = write_text_file(project_dir, path, content)
        return result if ok else f"ERROR: {result}"

    async def list_tasks(self, __metadata__: dict = {}) -> str:
        """
        Mostra el tauler de tasques compartit de la taula rodona (id, títol, estat, assignat).
        :return: Llista de tasques en text pla.
        """
        channel_id = _channel_id(__metadata__)
        if not channel_id:
            return "ERROR: no s'ha pogut identificar l'espai."
        return collab_tasks.tasks_as_text(await collab_tasks.get_tasks(channel_id))

    async def create_task(self, title: str, assignee: str = "", __metadata__: dict = {}) -> str:
        """
        Crea una tasca nova al tauler compartit de l'equip.
        :param title: Descripció curta i concreta de la tasca.
        :param assignee: Opcional, nom de l'agent o persona que se l'assigna.
        :return: Confirmació amb l'id de la tasca creada.
        """
        channel_id = _channel_id(__metadata__)
        if not channel_id:
            return "ERROR: no s'ha pogut identificar l'espai."
        task = await collab_tasks.create_task(
            channel_id, title, created_by=_agent_name(__metadata__), assignee=assignee
        )
        return f"Tasca creada [{task['id']}]: {task['title']}"

    async def update_task(
        self,
        task_id: str,
        status: str = "",
        assignee: str = "",
        notes: str = "",
        __metadata__: dict = {},
    ) -> str:
        """
        Actualitza una tasca del tauler compartit (canviar estat, assignar-se-la, afegir notes).
        :param task_id: Id de la tasca (el que surt entre claudàtors a list_tasks).
        :param status: Nou estat: "pending", "doing" o "done". Buit = no canviar.
        :param assignee: Nou assignat. Buit = no canviar.
        :param notes: Nota curta sobre l'estat de la feina. Buit = no canviar.
        :return: Confirmació o motiu de l'error.
        """
        channel_id = _channel_id(__metadata__)
        if not channel_id:
            return "ERROR: no s'ha pogut identificar l'espai."
        ok, result = await collab_tasks.update_task(
            channel_id, task_id, status=status, assignee=assignee, notes=notes
        )
        return result if ok else f"ERROR: {result}"

    async def read_conversation(self, offset: int = 0, limit: int = 60, __metadata__: dict = {}) -> str:
        """
        Llegeix l'historial COMPLET de la conversa de l'equip (tot queda guardat). Útil per revisar tot el que s'ha fet o repassar decisions antigues que ja no surten al context normal.
        :param offset: Quants missatges recents saltar (0 = els més nous). Augmenta'l per anar enrere en el temps.
        :param limit: Quants missatges retornar (màxim 200).
        :return: Tros de conversa en ordre cronològic amb els autors, i el total de missatges.
        """
        channel_id = _channel_id(__metadata__)
        if not channel_id:
            return "ERROR: no s'ha pogut identificar l'espai."
        return await collab_history.conversation_text(channel_id, offset=offset, limit=limit)

    async def search_conversation(self, query: str, limit: int = 20, __metadata__: dict = {}) -> str:
        """
        Busca un text dins de TOTA la conversa de l'equip (historial complet guardat).
        :param query: Text a buscar (no distingeix majúscules).
        :param limit: Màxim de missatges a retornar (fins a 50).
        :return: Els missatges que contenen el text, amb els autors, en ordre cronològic.
        """
        channel_id = _channel_id(__metadata__)
        if not channel_id:
            return "ERROR: no s'ha pogut identificar l'espai."
        return await collab_history.search_conversation(channel_id, query, limit=limit)

    async def propose_finish(self, summary: str, __metadata__: dict = {}) -> str:
        """
        Proposa donar la feina de l'equip per ACABADA. Els altres agents votaran; si hi ha consens, la ronda es tanca amb el teu resum final. Usa-ho només quan l'objectiu estigui realment complet.
        :param summary: Resum final de la feina feta i el resultat.
        :return: Confirmació que la proposta queda pendent de votació.
        """
        channel_id = _channel_id(__metadata__)
        if not channel_id:
            return "ERROR: no s'ha pogut identificar l'espai."
        await collab_tasks.set_end_proposal(channel_id, _agent_name(__metadata__), summary)
        return (
            "Proposta de tancament registrada. En acabar el teu torn, la resta "
            "d'agents votaran si la feina està acabada."
        )
'''


async def ensure_collab_tool(user_id: str) -> bool:
    """Registra (o actualitza) el tool `collab_files` a la BD. Idempotent:
    només reescriu quan la versió del contingut canvia."""
    try:
        existing = await Tools.get_tool_by_id(COLLAB_TOOL_ID)
        if existing and f"version: {COLLAB_TOOL_VERSION}" in existing.content:
            return True

        module, _frontmatter = await load_tool_module_by_id(COLLAB_TOOL_ID, content=TOOL_CONTENT)
        specs = get_tool_specs(module)

        if existing:
            updated = await Tools.update_tool_by_id(
                COLLAB_TOOL_ID, {"content": TOOL_CONTENT, "specs": specs}
            )
            return updated is not None

        created = await Tools.insert_new_tool(
            user_id,
            ToolForm(
                id=COLLAB_TOOL_ID,
                name="Fitxers del projecte (collab)",
                content=TOOL_CONTENT,
                meta=ToolMeta(
                    description=(
                        "Eines de fitxers de la taula rodona: llistar/llegir/escriure "
                        "la carpeta-projecte. Registrat automàticament pel mòdul collab."
                    )
                ),
            ),
            specs,
        )
        return created is not None
    except Exception:
        log.exception("No s'ha pogut registrar el tool %s", COLLAB_TOOL_ID)
        return False
