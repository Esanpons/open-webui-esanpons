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
# (CreateProcessAsUserW failed: 5), but chat is text-only, so we run read-only
# (Codex answers questions / reviews pasted code but does not touch files).

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

_SESSION_ID_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{8,})")


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
            description='model_reasoning_effort: "low" (fast), "medium", or "high" (deep, spends more quota). Avoid xhigh.',
        )
        MODEL: str = Field(
            default="gpt-5.5",
            description="Codex model. Must be one supported by a ChatGPT account (e.g. gpt-5.5). NOTE: gpt-5.1 and API-only models are rejected with a ChatGPT login.",
        )
        TIMEOUT_SECONDS: int = Field(
            default=300,
            description="Max seconds to wait for a Codex reply before giving up.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def pipes(self) -> List[Dict[str, str]]:
        return [{"id": "codex", "name": "Codex (ChatGPT Plus)"}]

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

        chat_id = __chat_id__ or "default"
        resume_sid = _chat_sessions.get(chat_id)

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
            f"model_reasoning_effort={self.valves.EFFORT}",
            "--output-last-message",
            msg_path,
        ]
        if self.valves.MODEL.strip():
            cmd += ["--model", self.valves.MODEL.strip()]
        if not resume_sid:
            # `resume` rejects -s; the sandbox goes via config on follow-ups.
            cmd += ["-s", "read-only"]
        cmd += ["-"]  # read the prompt from STDIN

        await emit_status(
            "🤖 Codex resumeix la sessió…" if resume_sid else "🤖 Codex pensa…"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")),
                    timeout=self.valves.TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await emit_status("Timeout.", done=True)
                yield f"\n\n**Codex error:** timed out after {self.valves.TIMEOUT_SECONDS}s.\n"
                return

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
            if sid:
                _chat_sessions[chat_id] = sid

            await emit_status("Done.", done=True)

            if answer:
                yield answer
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
