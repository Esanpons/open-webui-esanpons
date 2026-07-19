"""Tests del circuit breaker persistent — W5.1.

Prova els 3 estats (closed → open → half_open → closed), el cooldown amb
backoff, la persistència via collab_state i els edge cases.
"""

import asyncio
import importlib.metadata
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

import open_webui.collab.engine as collab_engine
from open_webui.collab.engine import (
    CollabState,
    get_state_value,
    set_state_value,
)
from open_webui.collab.circuit_breaker import (
    CircuitInfo,
    can_proceed,
    get_circuit,
    record_failure,
    record_success,
    reset_circuit,
    list_circuits,
    STATE_CLOSED,
    STATE_OPEN,
    STATE_HALF_OPEN,
    DEFAULT_THRESHOLD,
    DEFAULT_COOLDOWN,
    MAX_COOLDOWN,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

CHANNEL = "test-circuit-channel"
AGENT = "gpt-4o"


@pytest_asyncio.fixture(autouse=True)
async def temp_db(tmp_path, monkeypatch):
    """BD SQLite temporal amb la taula collab_state, injectada a l'engine.

    Cada test comença amb una BD nova, així no cal netejar l'estat del circuit.
    """
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'circuit.db'}")
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


# ------------------------------------------------------------------
# Estat inicial
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_state_is_closed():
    """Un agent nou té el circuit tancat i permet crides."""
    info = await get_circuit(CHANNEL, AGENT)
    assert info.state == STATE_CLOSED
    assert info.consecutive_failures == 0
    assert info.allows_call is True
    assert info.is_open is False
    assert info.is_half_open is False


@pytest.mark.asyncio
async def test_can_proceed_when_closed():
    """Un agent amb circuit tancat pot rebre crides."""
    assert await can_proceed(CHANNEL, AGENT) is True


# ------------------------------------------------------------------
# Acumulació d'errors
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_failure_stays_closed():
    """Un error no obre el circuit si no s'arriba al threshold."""
    info = await record_failure(CHANNEL, AGENT, "provider_error")
    assert info.state == STATE_CLOSED
    assert info.consecutive_failures == 1
    assert info.last_status == "provider_error"
    assert await can_proceed(CHANNEL, AGENT) is True


@pytest.mark.asyncio
async def test_two_failures_stay_closed():
    """Dos errors no obren el circuit (threshold = 3)."""
    await record_failure(CHANNEL, AGENT, "timeout")
    info = await record_failure(CHANNEL, AGENT, "timeout")
    assert info.state == STATE_CLOSED
    assert info.consecutive_failures == 2
    assert await can_proceed(CHANNEL, AGENT) is True


# ------------------------------------------------------------------
# Obertura del circuit
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threshold_failures_open_circuit():
    """Tres errors consecutius obren el circuit."""
    await record_failure(CHANNEL, AGENT, "quota_exceeded")
    await record_failure(CHANNEL, AGENT, "quota_exceeded")
    info = await record_failure(CHANNEL, AGENT, "quota_exceeded")
    assert info.state == STATE_OPEN
    assert info.consecutive_failures == 3
    assert info.last_status == "quota_exceeded"
    assert info.opened_at is not None


@pytest.mark.asyncio
async def test_open_circuit_blocks_calls():
    """Un circuit obert bloca les crides."""
    for _ in range(DEFAULT_THRESHOLD):
        await record_failure(CHANNEL, AGENT, "provider_error")
    assert await can_proceed(CHANNEL, AGENT) is False


@pytest.mark.asyncio
async def test_is_open_property():
    """is_open és True quan el circuit està obert i dins del cooldown."""
    for _ in range(DEFAULT_THRESHOLD):
        await record_failure(CHANNEL, AGENT, "provider_error")
    info = await get_circuit(CHANNEL, AGENT)
    assert info.is_open is True
    assert info.is_half_open is False


# ------------------------------------------------------------------
# Recovery
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_resets_circuit():
    """Un èxit tanca el circuit i reseteja els errors."""
    await record_failure(CHANNEL, AGENT, "provider_error")
    await record_failure(CHANNEL, AGENT, "provider_error")
    await record_success(CHANNEL, AGENT)
    info = await get_circuit(CHANNEL, AGENT)
    assert info.state == STATE_CLOSED
    assert info.consecutive_failures == 0
    assert info.last_status == "success"


# ------------------------------------------------------------------
# Half-open (cooldown passat)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_half_open_after_cooldown():
    """Després del cooldown, el circuit permet una crida de prova (half_open)."""
    # Obre el circuit
    for _ in range(DEFAULT_THRESHOLD):
        await record_failure(CHANNEL, AGENT, "quota_exceeded")

    # Simula que el cooldown ha passat: modifica opened_at al passat
    data = await get_state_value(CHANNEL, f"circuit:{AGENT}")
    data["opened_at"] = data["opened_at"] - (DEFAULT_COOLDOWN + 10)
    await set_state_value(CHANNEL, f"circuit:{AGENT}", data)

    info = await get_circuit(CHANNEL, AGENT)
    assert info.is_half_open is True
    assert info.is_open is False

    # can_proceed hauria de permetre i marcar com half_open
    result = await can_proceed(CHANNEL, AGENT)
    assert result is True

    # Verifica que el circuit està en half_open
    info = await get_circuit(CHANNEL, AGENT)
    assert info.state == STATE_HALF_OPEN


@pytest.mark.asyncio
async def test_half_open_success_closes_circuit():
    """Si la prova half_open té èxit, el circuit es tanca."""
    # Obre i simula cooldown passat
    for _ in range(DEFAULT_THRESHOLD):
        await record_failure(CHANNEL, AGENT, "provider_error")
    data = await get_state_value(CHANNEL, f"circuit:{AGENT}")
    data["opened_at"] = data["opened_at"] - (DEFAULT_COOLDOWN + 10)
    await set_state_value(CHANNEL, f"circuit:{AGENT}", data)

    # can_proceed marca com half_open
    await can_proceed(CHANNEL, AGENT)

    # Èxit → closed
    await record_success(CHANNEL, AGENT)
    info = await get_circuit(CHANNEL, AGENT)
    assert info.state == STATE_CLOSED
    assert info.consecutive_failures == 0


@pytest.mark.asyncio
async def test_half_open_failure_doubles_cooldown():
    """Si la prova half_open falla, el circuit es reobre amb cooldown doblat."""
    # Obre i simula cooldown passat
    for _ in range(DEFAULT_THRESHOLD):
        await record_failure(CHANNEL, AGENT, "provider_error")
    data = await get_state_value(CHANNEL, f"circuit:{AGENT}")
    data["opened_at"] = data["opened_at"] - (DEFAULT_COOLDOWN + 10)
    await set_state_value(CHANNEL, f"circuit:{AGENT}", data)

    # can_proceed marca com half_open
    await can_proceed(CHANNEL, AGENT)

    # Fallida → reobre amb cooldown doblat
    info = await record_failure(CHANNEL, AGENT, "provider_error")
    assert info.state == STATE_OPEN
    assert info.cooldown_seconds == DEFAULT_COOLDOWN * 2


# ------------------------------------------------------------------
# Reset manual
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_reset():
    """Reset manual tanca el circuit i neteja els errors."""
    for _ in range(DEFAULT_THRESHOLD):
        await record_failure(CHANNEL, AGENT, "provider_error")
    await reset_circuit(CHANNEL, AGENT)
    info = await get_circuit(CHANNEL, AGENT)
    assert info.state == STATE_CLOSED
    assert info.consecutive_failures == 0


# ------------------------------------------------------------------
# Persistència
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistence_across_reads():
    """L'estat del circuit es persisteix i sobreviu entre lectures."""
    await record_failure(CHANNEL, AGENT, "quota_exceeded")
    await record_failure(CHANNEL, AGENT, "quota_exceeded")

    # Simula un "reinici" tornant a llegir l'estat
    info = await get_circuit(CHANNEL, AGENT)
    assert info.consecutive_failures == 2
    assert info.state == STATE_CLOSED


# ------------------------------------------------------------------
# Llistat
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_circuits():
    """list_circuits retorna l'estat de tots els agents."""
    agents = ["gpt-4o", "claude-sonnet-4", "llama3"]
    for a in agents:
        await record_failure(CHANNEL, a, "timeout")

    circuits = await list_circuits(CHANNEL, agents)
    assert len(circuits) == 3
    for c in circuits:
        assert c.consecutive_failures == 1
        assert c.state == STATE_CLOSED


# ------------------------------------------------------------------
# CircuitInfo.to_dict
# ------------------------------------------------------------------


def test_circuit_info_to_dict():
    info = CircuitInfo(
        state=STATE_OPEN,
        consecutive_failures=3,
        last_status="quota_exceeded",
        opened_at=1700000000,
        cooldown_seconds=300,
    )
    d = info.to_dict()
    assert d["state"] == STATE_OPEN
    assert d["consecutive_failures"] == 3
    assert d["last_status"] == "quota_exceeded"
    assert d["opened_at"] == 1700000000
    assert d["cooldown_seconds"] == 300
    assert "is_open" in d
    assert "is_half_open" in d
    assert "allows_call" in d
