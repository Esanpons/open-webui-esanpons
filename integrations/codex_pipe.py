"""
title: Codex (ChatGPT Plus)
description: Run OpenAI Codex from inside OpenWebUI chats via the official `codex exec` CLI, billing against your ChatGPT Plus/Pro subscription (not the API).
author: esanpons (adapted from Esteve's parlar-amb-codex skill)
version: 0.1
license: MIT
"""

# How this works
# --------------
# OpenAI does NOT expose the ChatGPT Plus/Pro subscription through the API. The
# only official way to spend subscription quota is the `codex` CLI, which is
# authenticated via `codex login` (ChatGPT account) and stores its session in
# ~/.codex/auth.json. This pipe shells out to `codex exec` (the official
# non-interactive command) exactly like Esteve's `parlar-amb-codex` skill does,
# so every chat turn is answered by Codex using the subscription — no API key,
# no third-party proxy, no token handling.
#
# Per-chat sessions: Codex prints a "session id: <uuid>" on the first turn; we
# capture it and use `codex exec resume <sid>` on follow-ups so multi-turn
# context is preserved per OpenWebUI chat_id.
#
# Sandbox: this machine's Codex sandbox is broken under AzureAD
# (CreateProcessAsUserW failed: 5) — PowerShell child processes get denied, so
# Codex falls back to node_repl for file ops. It still works.
#
# Images (IMAGE_GENERATION valve): Codex has a built-in `image_gen` tool
# (gpt-image-2) billed against the ChatGPT subscription. It needs
# `image_generation = true` under [features] in ~/.codex/config.toml — without
# it Codex claims it will generate images and then silently doesn't.
# Plain chats get a writable folder under CACHE_DIR/codex_images/<chat_id> and
# run there with workspace-write (read-only before, which left generated images
# stranded in $CODEX_HOME). New images are detected after the turn and rendered
# inline via /cache/... (main.py: serve_cache_file). Collab turns with a project
# folder keep their own sandbox and are unaffected.

import asyncio
import logging
import os
import re
import shutil
import tempfile
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# chat_id -> codex_session_id, so follow-up turns resume the same Codex session.
_chat_sessions: Dict[str, str] = {}

# Màxim de CLIs `codex` concurrents des d'aquest backend (evita processos
# penjats per contenció quan diverses taules rodones treballen alhora).
_MAX_CONCURRENT = 2
_semaphore: "asyncio.Semaphore | None" = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore

_SESSION_ID_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{8,})")


def _collab_ctx(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Context de l'espai col·laboratiu (channels amb /collab). Arriba com a
    __metadata__['collab'] en crides directes (hand-raise) o com a
    __metadata__['variables']['collab'] via el pipeline complet (torns)."""
    md = metadata or {}
    ctx = md.get("collab") or (md.get("variables") or {}).get("collab") or {}
    return ctx if isinstance(ctx, dict) else {}


def _effective_timeout(collab: Dict[str, Any], normal_timeout: int, fallback: int) -> int | None:
    """Usa el guardrail del canal; 0 és sense límit i absència usa la valve."""
    if not collab:
        return normal_timeout
    if "turn_timeout" not in collab:
        return fallback
    try:
        configured = int(collab["turn_timeout"])
    except (TypeError, ValueError):
        return fallback
    return configured if configured > 0 else None


def _codex_base() -> List[str]:
    """Return the argv to invoke Codex with no shell.

    On Windows `codex` is a .CMD shim that subprocess can't exec directly, so we
    call the JS entrypoint with node (same trick as the parlar-amb-codex skill).
    """
    shim = shutil.which("codex")
    node = shutil.which("node") or "node"
    if shim:
        js = os.path.join(
            os.path.dirname(shim),
            "node_modules",
            "@openai",
            "codex",
            "bin",
            "codex.js",
        )
        if os.path.exists(js):
            return [node, js]
        if not shim.lower().endswith((".cmd", ".bat", ".ps1")):
            return [shim]  # Linux/Mac: real executable
    raise RuntimeError(
        "Codex CLI not found (neither the shim nor codex.js). Check `codex --version`."
    )


def _extract_system_prompt(body: Dict[str, Any]) -> str:
    """El pipeline (i l'espai col·laboratiu) passen instruccions com a missatge
    role=system; el CLI només rep el prompt per STDIN, així que cal
    incorporar-les-hi explícitament."""
    for message in body.get("messages") or []:
        if message.get("role") == "system":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def _images_dir(chat_id: str) -> Optional[str]:
    """Carpeta on Codex desa les imatges d'aquest xat.

    Ha de viure sota CACHE_DIR (= DATA_DIR/cache) perquè el backend només serveix
    fitxers d'allà per /cache/... (main.py: serve_cache_file, amb protecció de
    path traversal). Retorna None si no la podem crear."""
    try:
        from open_webui.config import CACHE_DIR

        safe = re.sub(r"[^A-Za-z0-9_-]", "_", chat_id or "default")[:64]
        d = os.path.join(str(CACHE_DIR), "codex_images", safe)
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        log.exception("no he pogut preparar la carpeta d'imatges de Codex")
        return None


def _new_images(directory: str, coneguts: set) -> List[str]:
    """Imatges noves aparegudes a `directory` (no recursiu) des de l'últim torn."""
    if not directory or not os.path.isdir(directory):
        return []
    exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    trobades = [
        f for f in os.listdir(directory)
        if f.lower().endswith(exts) and os.path.isfile(os.path.join(directory, f))
    ]
    return sorted(set(trobades) - coneguts)


def _markdown_images(chat_id: str, directory: str, noms: List[str]) -> str:
    """Enllaços markdown que el xat renderitza inline via /cache/..."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", chat_id or "default")[:64]
    parts = []
    for n in noms:
        # cache-buster: sense ell, reeditar una imatge amb el mateix nom mostra
        # la versió antiga del navegador.
        mtime = int(os.path.getmtime(os.path.join(directory, n)))
        parts.append(f"![{n}](/cache/codex_images/{safe}/{n}?v={mtime})")
    return "\n\n".join(parts)


def _extract_latest_user_prompt(body: Dict[str, Any]) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
    return ""


class Pipe:
    class Valves(BaseModel):
        EFFORT: str = Field(
            default="low",
            description='DEFAULT effort when the picked model doesn\'t specify one: "low", "medium", or "high". Normally you pick model+effort from the model selector instead.',
        )
        MODEL: str = Field(
            default="gpt-5.5",
            description="DEFAULT model (fallback). Normally you pick the model from the selector. ChatGPT-account models need CLI >= 0.144: gpt-5.6-sol / -terra / -luna, or gpt-5.5. API-only models (gpt-5.1) are rejected.",
        )
        TIMEOUT_SECONDS: int = Field(
            default=300,
            description="Max seconds to wait for a normal Codex chat reply.",
        )
        COLLAB_TIMEOUT_SECONDS: int = Field(
            default=570,
            description=(
                "Max seconds for a collaborative Codex turn. Must remain below "
                "the orchestrator hard timeout (600s) so cleanup stays coordinated."
            ),
        )
        IMAGE_GENERATION: bool = Field(
            default=True,
            description=(
                "Deixa que Codex generi imatges als xats normals (gpt-image-2, "
                "facturat contra la subscripció de ChatGPT). Les desa a "
                "DATA_DIR/cache/codex_images/<chat> i el xat les mostra inline. "
                "Requereix `image_generation = true` a [features] de "
                "~/.codex/config.toml. Amb això, els xats corren amb "
                "workspace-write limitat a la carpeta d'imatges (no read-only)."
            ),
        )
        COLLAB_SANDBOX: str = Field(
            default="danger-full-access",
            description=(
                "Sandbox de Codex quan el torn ve d'un espai col·laboratiu amb "
                "carpeta-projecte (perquè pugui editar fitxers): read-only, "
                "workspace-write o danger-full-access. En aquesta màquina el "
                "sandbox natiu falla sota AzureAD, per això el default és "
                "danger-full-access — Codex escriu al projecte SENSE sandbox: "
                "usa només carpetes de confiança. Els xats normals segueixen "
                "sent read-only."
            ),
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    # Models available to a ChatGPT Plus account via the Codex CLI (needs CLI
    # >= 0.144). id → display label. gpt-5.5 kept as a stable fallback.
    _MODELS = [
        ("gpt-5.6-sol", "Sol"),
        ("gpt-5.6-terra", "Terra"),
        ("gpt-5.6-luna", "Luna"),
        ("gpt-5.5", "5.5"),
    ]
    _EFFORTS = ["low", "medium", "high"]

    def pipes(self) -> List[Dict[str, str]]:
        # One clean entry per model. Effort is chosen separately via Open
        # WebUI's native "reasoning_effort" param (Chat Controls → Advanced),
        # read from the body in _resolve_choice.
        return [
            {"id": model_id, "name": f"Codex {label} (CLI)"}
            for model_id, label in self._MODELS
        ]

    def _resolve_choice(self, body: Dict[str, Any]) -> "tuple[str, str]":
        """Resolve (model, effort). Model comes from the picked pipe id; effort
        from Open WebUI's native `reasoning_effort` body param (Chat Controls →
        Advanced Params), falling back to the Valve defaults.

        Model ids contain dots (gpt-5.6-sol), so we match the picked pipe id
        against the known model list rather than split on '.'."""
        model = self.valves.MODEL.strip() or "gpt-5.5"
        effort = self.valves.EFFORT.strip() or "low"

        raw = str(body.get("model", "")) if isinstance(body, dict) else ""
        for model_id, _label in self._MODELS:
            if raw.endswith(model_id):
                model = model_id
                break

        native = body.get("reasoning_effort") if isinstance(body, dict) else None
        if isinstance(native, str) and native.strip().lower() in self._EFFORTS:
            effort = native.strip().lower()

        return model, effort

    async def pipe(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        prompt = _extract_latest_user_prompt(body)
        if not prompt.strip():
            yield "_No user message to send to Codex._"
            return

        system = _extract_system_prompt(body)
        if system:
            prompt = f"[Instruccions del sistema]\n{system}\n\n[Missatge]\n{prompt}"

        # Espai col·laboratiu: amb carpeta-projecte, Codex corre des d'allà
        # (com `codex` al terminal dins la carpeta) i amb sandbox d'escriptura.
        # Les crides de "mà alçada" (vols intervenir?) són one-shot, sense sessió.
        collab = _collab_ctx(__metadata__)
        timeout_seconds = _effective_timeout(
            collab, self.valves.TIMEOUT_SECONDS, self.valves.COLLAB_TIMEOUT_SECONDS
        )
        project_dir = collab.get("project_dir")
        if project_dir and not os.path.isdir(project_dir):
            project_dir = None
        is_handraise = collab.get("task") == "handraise"

        chat_id = __chat_id__ or collab.get("channel_id") or "default"
        session_key = f"{chat_id}|{project_dir}" if project_dir else chat_id

        resume_sid = None if is_handraise else _chat_sessions.get(session_key)
        model, effort = self._resolve_choice(body)

        # Imatges: als xats normals (sense carpeta-projecte) donem a Codex una
        # carpeta d'escriptura dins el cache. Sense això corria read-only i,
        # encara que generés la imatge, no la podia desar enlloc visible.
        img_dir = None
        img_abans: set = set()
        if self.valves.IMAGE_GENERATION and not project_dir and not is_handraise:
            img_dir = _images_dir(chat_id)
            if img_dir:
                img_abans = set(_new_images(img_dir, set()))
                prompt += (
                    "\n\n[Context de l'entorn]\n"
                    "Estàs responent dins d'un xat d'Open WebUI, no d'un terminal.\n"
                    f"Si generes o edites imatges, desa-les a: {img_dir}\n"
                    "Fes servir noms curts i descriptius (ex. cargol.png). "
                    "L'usuari les veurà al xat automàticament: no cal que li'n "
                    "donis la ruta ni que les copiïs enlloc més."
                )

        async def emit_status(description: str, done: bool = False) -> None:
            if __event_emitter__ is None:
                return
            await __event_emitter__(
                {"type": "status", "data": {"description": description, "done": done}}
            )

        try:
            base = _codex_base()
        except Exception as exc:
            yield f"\n\n**Codex error:** `{exc}`\n"
            return

        # Codex writes its final answer to --output-last-message; stdout carries
        # progress logs (and the session id). The prompt is fed via STDIN using
        # the `-` argument (robust against quoting/length/Windows issues), the
        # same approach as Esteve's SaPa-Connect service.
        fd, msg_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)

        cmd = base + ["exec"]
        if resume_sid:
            cmd += ["resume", resume_sid]
        cmd += [
            "--skip-git-repo-check",
            "-c",
            f"model_reasoning_effort={effort}",
            "--output-last-message",
            msg_path,
        ]
        if model:
            cmd += ["--model", model]
        if not resume_sid:
            # `resume` rejects -s; the sandbox goes via config on follow-ups.
            if project_dir:
                sandbox = self.valves.COLLAB_SANDBOX.strip() or "read-only"
            elif img_dir:
                # Xat normal amb imatges: pot escriure, però només a img_dir
                # (hi correm a dins, i workspace-write limita l'escriptura al cwd).
                sandbox = "workspace-write"
            else:
                sandbox = "read-only"
            cmd += ["-s", sandbox]
        cmd += ["-"]  # read the prompt from STDIN

        await emit_status(
            "🤖 Codex resumeix la sessió…" if resume_sid else "🤖 Codex pensa…"
        )

        try:
            async with _get_semaphore():
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    # carpeta-projecte > carpeta d'imatges > cwd del backend
                    cwd=project_dir or img_dir,
                )
                try:
                    stdout_bytes, _ = await asyncio.wait_for(
                        proc.communicate(input=prompt.encode("utf-8")),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await emit_status("Timeout.", done=True)
                    yield f"\n\n**Codex error:** timed out after {timeout_seconds}s.\n"
                    return
                except asyncio.CancelledError:
                    proc.kill()
                    raise

            logs = (stdout_bytes or b"").decode("utf-8", errors="replace")

            answer = ""
            try:
                with open(msg_path, encoding="utf-8", errors="replace") as f:
                    answer = f.read().strip()
            except OSError:
                pass

            # Capture / refresh the Codex session id for follow-up turns.
            match = _SESSION_ID_RE.search(logs)
            sid = match.group(1) if match else resume_sid
            if sid and not is_handraise:
                _chat_sessions[session_key] = sid

            # Imatges noves generades en aquest torn → markdown inline.
            imatges = ""
            if img_dir:
                noves = _new_images(img_dir, img_abans)
                if noves:
                    imatges = "\n\n" + _markdown_images(chat_id, img_dir, noves)

            await emit_status("Done.", done=True)

            if answer:
                yield answer + imatges
            elif imatges:
                # Ha generat la imatge però no ha dit res: la imatge ja és la resposta.
                yield imatges.lstrip()
            else:
                tail = logs[-1500:] if logs else "(no output)"
                yield (
                    "\n\n**Codex no ha retornat resposta.**\n\n"
                    f"<details>\n<summary>Sortida de Codex</summary>\n\n```\n{tail}\n```\n\n</details>\n"
                )
        except Exception as exc:
            log.exception("Codex pipe failed")
            import traceback as _tb

            await emit_status(f"Error: {exc}", done=True)
            yield f"\n\n**Codex error:** `{type(exc).__name__}: {exc}`\n"
            yield f"\n\n<details>\n<summary>Traceback</summary>\n\n```\n{_tb.format_exc()}\n```\n\n</details>\n"
        finally:
            try:
                os.remove(msg_path)
            except OSError:
                pass
