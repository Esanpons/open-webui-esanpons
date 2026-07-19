"""Tests de validació de models (W5.3 / S3) i cicle complet del circuit breaker.

Prova:
- _validate_models() amb models vàlids, invàlids, buits i fail-open
  (contrasta amb request.app.state.MODELS, la mateixa font que main.py)
- Cicle complet del circuit breaker via collab_state (BD temporal)
"""

import importlib.metadata
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

import open_webui.collab.engine as collab_engine
from open_webui.collab.engine import CollabState
from open_webui.collab.router import _validate_models


def _request_with_models(models):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(MODELS=models)))


# ------------------------------------------------------------------
# _validate_models (S3)
# ------------------------------------------------------------------


class TestValidateModels:
    @pytest.mark.asyncio
    async def test_all_valid(self):
        """Tots els model_ids existeixen → llista buida d'invàlids."""
        request = _request_with_models({"gpt-4o": {}, "claude-sonnet-4": {}, "llama3": {}})
        assert await _validate_models(request, ["gpt-4o", "claude-sonnet-4"]) == []

    @pytest.mark.asyncio
    async def test_some_invalid(self):
        """Alguns model_ids no existeixen → apareixen a la llista."""
        request = _request_with_models({"gpt-4o": {}})
        invalid = await _validate_models(request, ["gpt-4o", "nonexistent-model", "also-fake"])
        assert "nonexistent-model" in invalid
        assert "also-fake" in invalid
        assert "gpt-4o" not in invalid

    @pytest.mark.asyncio
    async def test_empty_list(self):
        """Llista buida → sempre vàlida."""
        request = _request_with_models({"gpt-4o": {}})
        assert await _validate_models(request, []) == []

    @pytest.mark.asyncio
    async def test_fail_open_when_models_not_loaded(self):
        """Si MODELS encara no està poblat (startup), no bloqueja (fail-open)."""
        request = _request_with_models({})
        assert await _validate_models(request, ["gpt-4o", "claude-sonnet-4"]) == []

    @pytest.mark.asyncio
    async def test_fail_open_on_error(self):
        """Si accedir a MODELS falla, no bloqueja (fail-open)."""
        class Boom:
            @property
            def MODELS(self):
                raise RuntimeError("state down")

        request = SimpleNamespace(app=SimpleNamespace(state=Boom()))
        assert await _validate_models(request, ["gpt-4o"]) == []

    @pytest.mark.asyncio
    async def test_case_sensitive(self):
        """La validació distingeix majúscules i minúscules."""
        request = _request_with_models({"gpt-4o": {}})
        invalid = await _validate_models(request, ["GPT-4O", "gpt-4o"])
        assert "GPT-4O" in invalid
        assert "gpt-4o" not in invalid


# ------------------------------------------------------------------
# Circuit breaker state lifecycle (integration test via module)
# ------------------------------------------------------------------


@pytest_asyncio.fixture
async def temp_db(tmp_path, monkeypatch):
    """BD SQLite temporal amb collab_state injectada a l'engine."""
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'w5.db'}")
    async with db_engine.begin() as connection:
        await connection.run_sync(CollabState.__table__.create)
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def _ctx():
        async with sessions() as session:
            yield session

    monkeypatch.setattr(collab_engine, "get_async_db_context", _ctx)
    yield
    await db_engine.dispose()


class TestCircuitBreakerLifecycle:
    """Tests que proven el cicle complet del circuit breaker via collab_state."""

    @pytest.mark.asyncio
    async def test_full_cycle_closed_open_half_closed(self, temp_db):
        """Cicle complet: closed → open → half_open → closed."""
        from open_webui.collab.circuit_breaker import (
            record_failure,
            record_success,
            can_proceed,
            get_circuit,
            STATE_CLOSED,
            STATE_OPEN,
            STATE_HALF_OPEN,
            DEFAULT_THRESHOLD,
            DEFAULT_COOLDOWN,
        )
        from open_webui.collab.engine import set_state_value, get_state_value

        channel = "test-s3-channel"
        agent = "test-agent-s3"

        # Closed
        assert await can_proceed(channel, agent) is True

        # Acumula errors fins obrir
        for _ in range(DEFAULT_THRESHOLD):
            info = await record_failure(channel, agent, "quota_exceeded")

        assert info.state == STATE_OPEN

        # Open → bloqueja
        assert await can_proceed(channel, agent) is False

        # Simula cooldown passat
        data = await get_state_value(channel, f"circuit:{agent}")
        data["opened_at"] = data["opened_at"] - (DEFAULT_COOLDOWN + 10)
        await set_state_value(channel, f"circuit:{agent}", data)

        # Half_open → permet
        assert await can_proceed(channel, agent) is True
        info = await get_circuit(channel, agent)
        assert info.state == STATE_HALF_OPEN

        # Success → closed
        await record_success(channel, agent)
        info = await get_circuit(channel, agent)
        assert info.state == STATE_CLOSED
