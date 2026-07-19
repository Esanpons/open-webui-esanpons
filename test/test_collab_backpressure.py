"""Tests del backpressure global i per proveïdor — W5.2.

Prova els semàfors, el context manager acquire(), la classificació de
proveïdors i els límits.
"""

import asyncio
import importlib.metadata
import pytest

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab.backpressure import (
    acquire,
    configure,
    stats,
    _provider_prefix,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_PER_PROVIDER,
)


# ------------------------------------------------------------------
# Classificació de proveïdors
# ------------------------------------------------------------------


class TestProviderPrefix:
    def test_openai(self):
        assert _provider_prefix("gpt-4o") == "openai"
        assert _provider_prefix("gpt-4o-mini") == "openai"
        assert _provider_prefix("o1-preview") == "openai"
        assert _provider_prefix("o3-mini") == "openai"
        assert _provider_prefix("o4-mini") == "openai"

    def test_anthropic(self):
        assert _provider_prefix("claude-sonnet-4") == "anthropic"
        assert _provider_prefix("claude-3-5-haiku") == "anthropic"
        assert _provider_prefix("claude-opus-4") == "anthropic"

    def test_google(self):
        assert _provider_prefix("gemini-2.0-flash") == "google"
        assert _provider_prefix("gemini-1.5-pro") == "google"

    def test_deepseek(self):
        assert _provider_prefix("deepseek-chat") == "deepseek"
        assert _provider_prefix("deepseek-reasoner") == "deepseek"

    def test_local(self):
        assert _provider_prefix("llama3") == "local"
        assert _provider_prefix("mistral-7b") == "local"
        assert _provider_prefix("qwen2.5") == "local"
        assert _provider_prefix("phi-3") == "local"

    def test_empty(self):
        assert _provider_prefix("") == "local"
        assert _provider_prefix(None) == "local"

    def test_unknown(self):
        assert _provider_prefix("some-custom-model") == "other"

    def test_open_webui_connection_prefixes(self):
        assert _provider_prefix("Groq.openai/gpt-oss-120b") == "groq"
        assert _provider_prefix("Groq.groq/compound") == "groq"
        assert _provider_prefix("OpenRouter.nvidia/nemotron:free") == "openrouter"
        assert _provider_prefix("Ollama.qwen2.5-coder:14b") == "ollama"
        assert _provider_prefix("ZenMux.z-ai/glm-4.7-flash-free") == "zenmux"


# ------------------------------------------------------------------
# acquire() context manager
# ------------------------------------------------------------------


class TestAcquire:
    @pytest.fixture(autouse=True)
    def setup_backpressure(self):
        """Configura amb valors petits per poder provar els límits."""
        configure(max_concurrent=3, max_per_provider=2)
        yield
        configure(max_concurrent=DEFAULT_MAX_CONCURRENT, max_per_provider=DEFAULT_MAX_PER_PROVIDER)

    @pytest.mark.asyncio
    async def test_acquire_and_release(self):
        """Adquireix i allibera un slot sense problemes."""
        async with acquire("gpt-4o"):
            s = stats()
            assert s["global"]["available"] is not None
            assert s["global"]["available"] < 3  # n'hi ha un menys
        # Després de sortir, tot alliberat
        s = stats()
        assert s["global"]["available"] == 3

    @pytest.mark.asyncio
    async def test_global_limit(self):
        """No es poden excedir les crides concurrents globals."""
        started = asyncio.Event()
        second_done = asyncio.Event()

        async def hold(slot, model_id="gpt-4o"):
            async with acquire(model_id):
                started.set()
                await asyncio.sleep(0.2)

        # Omple els 3 slots globals
        tasks = [asyncio.create_task(hold(i)) for i in range(3)]
        await asyncio.sleep(0.05)

        # El 4t hauria d'esperar
        fourth_started = asyncio.Event()

        async def fourth():
            async with acquire("gpt-4o"):
                fourth_started.set()

        t4 = asyncio.create_task(fourth())
        await asyncio.sleep(0.05)
        assert not fourth_started.is_set()  # encara esperant

        # Quan els 3 acabin, el 4t pot començar
        await asyncio.gather(*tasks)
        await asyncio.sleep(0.05)
        assert fourth_started.is_set()
        await t4

    @pytest.mark.asyncio
    async def test_provider_limit(self):
        """El semàfor per proveïdor limita independentment del global."""
        # Configuració: global=3, per_proveïdor=1
        configure(max_concurrent=3, max_per_provider=1)

        first_acquired = asyncio.Event()
        second_blocked = asyncio.Event()

        async def hold_provider():
            async with acquire("gpt-4o"):
                first_acquired.set()
                await asyncio.sleep(0.2)

        async def try_same_provider():
            await asyncio.sleep(0.05)
            try:
                async with asyncio.timeout(0.1):
                    async with acquire("gpt-4o"):
                        pass
            except asyncio.TimeoutError:
                second_blocked.set()

        t1 = asyncio.create_task(hold_provider())
        t2 = asyncio.create_task(try_same_provider())
        await asyncio.gather(t1, t2)

        assert first_acquired.is_set()
        assert second_blocked.is_set()  # Bloquejat pel semàfor del proveïdor

    @pytest.mark.asyncio
    async def test_different_providers_parallel(self):
        """Diferents proveïdors poden córrer en paral·lel."""
        configure(max_concurrent=10, max_per_provider=1)

        results = []

        async def call(model_id):
            async with acquire(model_id):
                results.append(model_id)

        # openai i anthropic haurien de poder córrer en paral·lel
        await asyncio.gather(
            call("gpt-4o"),
            call("claude-sonnet-4"),
        )
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_release_on_exception(self):
        """Si una crida falla dins del context, el slot s'allibera."""
        configure(max_concurrent=2, max_per_provider=2)

        try:
            async with acquire("gpt-4o"):
                raise ValueError("boom")
        except ValueError:
            pass

        # El slot s'hauria d'haver alliberat
        s = stats()
        assert s["global"]["available"] == 2


# ------------------------------------------------------------------
# stats()
# ------------------------------------------------------------------


class TestStats:
    @pytest.fixture(autouse=True)
    def setup(self):
        configure(max_concurrent=5, max_per_provider=3)
        yield
        configure(max_concurrent=DEFAULT_MAX_CONCURRENT, max_per_provider=DEFAULT_MAX_PER_PROVIDER)

    def test_stats_structure(self):
        s = stats()
        assert "global" in s
        assert "providers" in s
        assert s["global"]["max"] == 5

    @pytest.mark.asyncio
    async def test_stats_after_acquire(self):
        async with acquire("gpt-4o"):
            s = stats()
            assert "openai" in s["providers"]
            assert s["providers"]["openai"]["max"] == 3
