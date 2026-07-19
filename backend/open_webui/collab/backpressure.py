"""Backpressure global i per proveïdor per crides a models — W5.2.

Limita el nombre de crides concurrents a APIs de models per evitar saturar
el sistema quan múltiples canals activen rondes simultàniament.

Dos nivells:
1. **Semàfor global:** límit total de crides concurrents.
2. **Semàfor per proveïdor:** límit per prefix de model (openai, anthropic, …).

Disseny: ``docs/disseny-w5-w8-salut-ux-mantenibilitat-seguretat.md`` §5.2.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Configuració (valors per defecte conservadors)
# ------------------------------------------------------------------

DEFAULT_MAX_CONCURRENT = 10      # global
DEFAULT_MAX_PER_PROVIDER = 5     # per proveïdor

# ------------------------------------------------------------------
# Semàfors globals (singleton lazy)
# ------------------------------------------------------------------

_global_semaphore: Optional[asyncio.Semaphore] = None
_provider_semaphores: dict[str, asyncio.Semaphore] = {}
_max_concurrent = DEFAULT_MAX_CONCURRENT
_max_per_provider = DEFAULT_MAX_PER_PROVIDER


def configure(
    *,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    max_per_provider: int = DEFAULT_MAX_PER_PROVIDER,
) -> None:
    """Configura els límits.  Ha de cridar-se abans del primer ús (p. ex. a l'arrencada).

    Si ja hi ha semàfors creats, es reconstrueixen amb els nous valors.
    """
    global _global_semaphore, _max_concurrent, _max_per_provider
    _max_concurrent = max_concurrent
    _max_per_provider = max_per_provider
    _global_semaphore = asyncio.Semaphore(max_concurrent)
    _provider_semaphores.clear()
    log.info(
        "Backpressure configurada: %d global, %d per proveïdor",
        max_concurrent,
        max_per_provider,
    )


def _get_global() -> asyncio.Semaphore:
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(_max_concurrent)
    return _global_semaphore


def _provider_prefix(model_id: str) -> str:
    """Extreu el prefix de proveïdor d'un model_id.

    Exemples:
        ``gpt-4o`` → ``openai``
        ``claude-sonnet-4`` → ``anthropic``
        ``gemini-2.0-flash`` → ``google``
        ``deepseek-chat`` → ``deepseek``
        ``llama3`` → ``local``
    """
    if not model_id:
        return "local"
    mid = model_id.lower()
    # Open WebUI anteposa sovint el nom de la connexió al model real
    # (``Groq.openai/gpt-oss-120b``, ``OpenRouter.nvidia/...``). Sense
    # reconèixer-lo, totes aquestes APIs acabaven al mateix calaix ``other``.
    connection_prefixes = {
        "groq.": "groq",
        "openrouter.": "openrouter",
        "ollama.": "ollama",
        "zenmux.": "zenmux",
        "z.ai.": "z.ai",
        "google gemini.": "google",
    }
    for prefix, provider in connection_prefixes.items():
        if mid.startswith(prefix):
            return provider
    if mid.startswith(("gpt-", "o1", "o3", "o4", "openai")):
        return "openai"
    if mid.startswith(("claude", "anthropic")):
        return "anthropic"
    if mid.startswith(("gemini", "google")):
        return "google"
    if mid.startswith("deepseek"):
        return "deepseek"
    if mid.startswith(("llama", "mistral", "qwen", "phi")):
        return "local"
    return "other"


def _get_provider_semaphore(provider: str) -> asyncio.Semaphore:
    """Obté (o crea) el semàfor per un proveïdor concret."""
    sem = _provider_semaphores.get(provider)
    if sem is None:
        sem = asyncio.Semaphore(_max_per_provider)
        _provider_semaphores[provider] = sem
    return sem


@asynccontextmanager
async def acquire(model_id: str = ""):
    """Adquireix un slot de backpressure per a una crida a model.

    Ús::

        async with backpressure.acquire(model_id):
            response = await generate_completion(...)

    Bloqueja fins que hi ha capacitat disponible.
    """
    global_sem = _get_global()
    provider = _provider_prefix(model_id)
    provider_sem = _get_provider_semaphore(provider)

    await global_sem.acquire()
    try:
        await provider_sem.acquire()
        try:
            yield
        finally:
            provider_sem.release()
    finally:
        global_sem.release()


def stats() -> dict:
    """Retorna estadístiques dels semàfors (per observabilitat/debug)."""
    provider_stats = {}
    for provider, sem in _provider_semaphores.items():
        provider_stats[provider] = {
            "max": _max_per_provider,
            "available": sem._value if hasattr(sem, "_value") else None,
        }
    global_sem = _global_semaphore
    return {
        "global": {
            "max": _max_concurrent,
            "available": global_sem._value if global_sem and hasattr(global_sem, "_value") else None,
        },
        "providers": provider_stats,
    }
