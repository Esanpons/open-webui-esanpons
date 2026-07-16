"""
title: Claude (CLI · Max)
description: Run Claude from inside OpenWebUI chats via the official `claude -p` CLI, using the account already logged in (no OAuth token to paste). Bills against your Claude Max/Pro subscription.
author: esanpons (adapted from Esteve's SaPa-Connect ai.py)
version: 0.1
license: MIT
"""

# How this works
# --------------
# The `claude` CLI (Claude Code) is already authenticated on this machine
# (`claude` login). This pipe shells out to `claude -p` (print / non-interactive
# mode) and feeds the prompt via STDIN — the same approach as Esteve's
# SaPa-Connect service and parlar-amb-* skills. No OAuth token to generate or
# paste, no SDK dependency: it just reuses the logged-in session, spending the
# Max/Pro subscription.
#
# This is the simpler sibling of claude_agent_pipe.py (which uses
# claude-agent-sdk + a setup-token). Use this one for plain chat; use the agent
# pipe when you want the full Claude Code tool loop.
#
# Per-chat sessions: we mint a stable session UUID per OpenWebUI chat_id and
# pass it with --session-id, then --resume it on follow-up turns so multi-turn
# context is preserved.

import asyncio
import logging
import os
import shutil
import uuid
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# chat_id -> session uuid, so follow-up turns resume the same Claude session.
_chat_sessions: Dict[str, str] = {}

# Màxim de CLIs `claude` concurrents des d'aquest backend. Sense límit, les
# taules rodones amb diversos canals actius poden llançar >10 processos alhora
# que es bloquegen entre ells (locks del CLI) i queden penjats per sempre.
_MAX_CONCURRENT = 2
_semaphore: "asyncio.Semaphore | None" = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


def _collab_ctx(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Context de l'espai col·laboratiu (channels amb /collab). Arriba com a
    __metadata__['collab'] en crides directes (hand-raise) o com a
    __metadata__['variables']['collab'] via el pipeline complet (torns)."""
    md = metadata or {}
    ctx = md.get("collab") or (md.get("variables") or {}).get("collab") or {}
    return ctx if isinstance(ctx, dict) else {}


def _resolve_claude() -> List[str]:
    """Return the argv prefix to invoke the claude CLI, handling the Windows
    .CMD shim (subprocess can't exec it directly → route through cmd /c), the
    same way SaPa-Connect's _build_argv does."""
    exe = shutil.which("claude") or "claude"
    if os.name == "nt":
        low = exe.lower()
        if low.endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", exe]
        if low.endswith((".cmd", ".bat")):
            return ["cmd", "/c", exe]
    return [exe]


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
    # Model aliases the CLI accepts. id → display label.
    _MODELS = [
        ("opus", "Opus"),
        ("sonnet", "Sonnet"),
        ("haiku", "Haiku"),
        ("fable", "Fable"),
    ]
    # Effort levels the CLI accepts (--effort). Chosen via Open WebUI's native
    # reasoning_effort param (Chat Controls → Advanced).
    _EFFORTS = ["low", "medium", "high", "xhigh", "max"]

    class Valves(BaseModel):
        MODEL: str = Field(
            default="sonnet",
            description="DEFAULT Claude model alias (fallback): opus / sonnet / haiku / fable. Normally you pick model+effort from the selector.",
        )
        EFFORT: str = Field(
            default="medium",
            description="DEFAULT effort (fallback): low / medium / high / xhigh / max. Normally picked from the selector.",
        )
        TIMEOUT_SECONDS: int = Field(
            default=300,
            description="Max seconds to wait for a Claude reply before giving up.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> List[Dict[str, str]]:
        # One clean entry per model. Effort is chosen separately via Open
        # WebUI's native "reasoning_effort" param (Chat Controls → Advanced),
        # read from the body in _resolve_choice.
        return [
            {"id": model_id, "name": f"Claude {label} (CLI)"}
            for model_id, label in self._MODELS
        ]

    def _resolve_choice(self, body: Dict[str, Any]) -> "tuple[str, str]":
        """Resolve (model, effort). Model comes from the picked pipe id;
        effort from Open WebUI's native `reasoning_effort` body param (Chat
        Controls → Advanced Params), falling back to the Valve defaults."""
        model = self.valves.MODEL.strip() or "sonnet"
        effort = self.valves.EFFORT.strip() or "medium"

        raw = str(body.get("model", "")) if isinstance(body, dict) else ""
        pipe_id = raw.split(".")[-1]  # aliases have no dots
        if pipe_id in {m for m, _ in self._MODELS}:
            model = pipe_id

        # Native reasoning_effort param (string). Only accept known levels.
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
            yield "_No user message to send to Claude._"
            return

        system = _extract_system_prompt(body)
        if system:
            prompt = f"[Instruccions del sistema]\n{system}\n\n[Missatge]\n{prompt}"

        # Espai col·laboratiu: amb carpeta-projecte, Claude corre des d'allà
        # (com `claude` al terminal dins la carpeta). Les crides de "mà alçada"
        # (vols intervenir?) són one-shot: sense sessió, per no embrutar la
        # conversa del canal.
        collab = _collab_ctx(__metadata__)
        project_dir = collab.get("project_dir")
        if project_dir and not os.path.isdir(project_dir):
            project_dir = None
        is_handraise = collab.get("task") == "handraise"

        chat_id = __chat_id__ or collab.get("channel_id") or "default"
        session_key = f"{chat_id}|{project_dir}" if project_dir else chat_id

        model, effort = self._resolve_choice(body)

        async def emit_status(description: str, done: bool = False) -> None:
            if __event_emitter__ is None:
                return
            await __event_emitter__(
                {"type": "status", "data": {"description": description, "done": done}}
            )

        cmd = _resolve_claude() + ["-p", "--permission-mode", "bypassPermissions"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]

        # Session handling: reuse the same session per chat so context carries.
        # Handraise: one-shot, cap sessió.
        existing_sid = None if is_handraise else _chat_sessions.get(session_key)
        if existing_sid:
            cmd += ["--resume", existing_sid]
            await emit_status(f"🧠 Claude {model} · {effort} (continuant)…")
        elif is_handraise:
            await emit_status(f"✋ Claude {model} decideix si intervé…")
        else:
            new_sid = str(uuid.uuid4())
            cmd += ["--session-id", new_sid]
            _chat_sessions[session_key] = new_sid
            await emit_status(f"🧠 Claude {model} · {effort}…")

        try:
            async with _get_semaphore():
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=project_dir,  # None → cwd del backend, com sempre
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(
                        proc.communicate(input=prompt.encode("utf-8")),
                        timeout=self.valves.TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await emit_status("Timeout.", done=True)
                    yield f"\n\n**Claude error:** timed out after {self.valves.TIMEOUT_SECONDS}s.\n"
                    return
                except asyncio.CancelledError:
                    # Qui ens crida (p.ex. el timeout de mà alçada de la taula
                    # rodona) ha cancel·lat: mata el CLI perquè no quedi zombi.
                    proc.kill()
                    raise

            answer = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
            errtext = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()

            await emit_status("Done.", done=True)

            if answer:
                yield answer
            else:
                tail = (errtext or "(no output)")[-1500:]
                yield (
                    "\n\n**Claude no ha retornat resposta.**\n\n"
                    f"<details>\n<summary>Sortida (stderr)</summary>\n\n```\n{tail}\n```\n\n</details>\n"
                )
                # If resume failed (stale session), drop it so the next turn starts fresh.
                _chat_sessions.pop(session_key, None)
        except Exception as exc:
            log.exception("Claude CLI pipe failed")
            import traceback as _tb

            await emit_status(f"Error: {exc}", done=True)
            yield f"\n\n**Claude error:** `{type(exc).__name__}: {exc}`\n"
            yield f"\n\n<details>\n<summary>Traceback</summary>\n\n```\n{_tb.format_exc()}\n```\n\n</details>\n"
