"""Circuit breaker persistent per agents col·laboratius — W5.1.

Un agent que acumula errors consecutius (p. ex. ``quota_exceeded``) es marca com
a ``circuit_open`` i no rep més crides fins que passa un *cooldown*.  Després
del cooldown, una única crida de prova (*half_open*) determina si es recupera.

L'estat es persisteix a ``collab_state`` (clau ``circuit:{agent_id}``) perquè
sobrevisqui a reinicis del servidor.

Disseny: ``docs/disseny-w5-w8-salut-ux-mantenibilitat-seguretat.md`` §5.1.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from open_webui.collab.engine import get_state_value, set_state_value

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

DEFAULT_THRESHOLD = 3          # errors consecutius per obrir el circuit
DEFAULT_COOLDOWN = 300         # 5 minuts
MAX_COOLDOWN = 3600            # 1 hora màxim
COOLDOWN_BACKOFF = 2           # doblem el cooldown cada obertura successiva

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"
VALID_STATES = (STATE_CLOSED, STATE_OPEN, STATE_HALF_OPEN)


# ------------------------------------------------------------------
# Dataclass públic
# ------------------------------------------------------------------


@dataclass
class CircuitInfo:
    """Snapshot immutable de l'estat del circuit d'un agent."""

    state: str
    consecutive_failures: int
    last_status: Optional[str]
    opened_at: Optional[int]
    cooldown_seconds: int

    @property
    def is_open(self) -> bool:
        """True si el circuit està obert i dins del cooldown."""
        if self.state == STATE_OPEN:
            if self.opened_at is not None:
                elapsed = int(time.time()) - self.opened_at
                return elapsed < self.cooldown_seconds
            return True
        return False

    @property
    def is_half_open(self) -> bool:
        """True si el circuit està en mode prova (cooldown passat)."""
        if self.state == STATE_OPEN and self.opened_at is not None:
            elapsed = int(time.time()) - self.opened_at
            return elapsed >= self.cooldown_seconds
        return self.state == STATE_HALF_OPEN

    @property
    def allows_call(self) -> bool:
        """True si l'agent pot rebre una crida ara mateix."""
        if self.state == STATE_CLOSED:
            return True
        if self.is_half_open:
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "last_status": self.last_status,
            "opened_at": self.opened_at,
            "cooldown_seconds": self.cooldown_seconds,
            "is_open": self.is_open,
            "is_half_open": self.is_half_open,
            "allows_call": self.allows_call,
        }


def _default_circuit() -> dict:
    return {
        "state": STATE_CLOSED,
        "consecutive_failures": 0,
        "last_status": None,
        "opened_at": None,
        "cooldown_seconds": DEFAULT_COOLDOWN,
    }


def _circuit_key(agent_id: str) -> str:
    return f"circuit:{agent_id}"


def _to_info(data: Optional[dict]) -> CircuitInfo:
    if not data:
        return CircuitInfo(
            state=STATE_CLOSED,
            consecutive_failures=0,
            last_status=None,
            opened_at=None,
            cooldown_seconds=DEFAULT_COOLDOWN,
        )
    return CircuitInfo(
        state=data.get("state", STATE_CLOSED),
        consecutive_failures=data.get("consecutive_failures", 0),
        last_status=data.get("last_status"),
        opened_at=data.get("opened_at"),
        cooldown_seconds=data.get("cooldown_seconds", DEFAULT_COOLDOWN),
    )


# ------------------------------------------------------------------
# Operacions públiques
# ------------------------------------------------------------------


async def get_circuit(channel_id: str, agent_id: str) -> CircuitInfo:
    """Llegeix l'estat actual del circuit d'un agent."""
    data = await get_state_value(channel_id, _circuit_key(agent_id))
    return _to_info(data)


async def can_proceed(channel_id: str, agent_id: str) -> bool:
    """Comprova si un agent pot rebre una crida ara mateix.

    Si el circuit està ``open`` però el cooldown ha passat, es marca
    automàticament com ``half_open`` (prova) i retorna ``True``.
    """
    info = await get_circuit(channel_id, agent_id)
    if info.is_half_open:
        # Transició open → half_open: permet una crida de prova.
        data = _default_circuit()
        data["state"] = STATE_HALF_OPEN
        data["consecutive_failures"] = info.consecutive_failures
        data["last_status"] = info.last_status
        data["opened_at"] = info.opened_at
        data["cooldown_seconds"] = info.cooldown_seconds
        await set_state_value(channel_id, _circuit_key(agent_id), data)
        log.info("Circuit %s: open → half_open (cooldown passat)", agent_id)
        return True
    if info.allows_call:
        return True
    return False


async def record_success(channel_id: str, agent_id: str) -> None:
    """Registra un èxit: reseteja el circuit a ``closed``."""
    data = _default_circuit()
    data["state"] = STATE_CLOSED
    data["consecutive_failures"] = 0
    data["last_status"] = "success"
    await set_state_value(channel_id, _circuit_key(agent_id), data)
    log.debug("Circuit %s: success → closed", agent_id)


async def record_failure(
    channel_id: str,
    agent_id: str,
    error_status: str,
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> CircuitInfo:
    """Registra un error.  Si s'arriba al threshold, obre el circuit.

    Si ja estava ``half_open``, torna a ``open`` amb cooldown doblat.
    """
    info = await get_circuit(channel_id, agent_id)
    now = int(time.time())

    if info.state == STATE_HALF_OPEN:
        # La prova de recuperació ha fallat: torna a open amb cooldown doblat.
        new_cooldown = min(info.cooldown_seconds * COOLDOWN_BACKOFF, MAX_COOLDOWN)
        data = {
            "state": STATE_OPEN,
            "consecutive_failures": info.consecutive_failures + 1,
            "last_status": error_status,
            "opened_at": now,
            "cooldown_seconds": new_cooldown,
        }
        await set_state_value(channel_id, _circuit_key(agent_id), data)
        log.warning(
            "Circuit %s: half_open → open (prova fallida, cooldown=%ds)",
            agent_id,
            new_cooldown,
        )
        return _to_info(data)

    # Estat closed o no inicialitzat.
    failures = info.consecutive_failures + 1
    if failures >= threshold:
        data = {
            "state": STATE_OPEN,
            "consecutive_failures": failures,
            "last_status": error_status,
            "opened_at": now,
            "cooldown_seconds": DEFAULT_COOLDOWN,
        }
        await set_state_value(channel_id, _circuit_key(agent_id), data)
        log.warning(
            "Circuit %s: closed → open (%d errors consecutius, status=%s)",
            agent_id,
            failures,
            error_status,
        )
        return _to_info(data)

    # Encara no s'arriba al threshold: acumula.
    data = {
        "state": STATE_CLOSED,
        "consecutive_failures": failures,
        "last_status": error_status,
        "opened_at": None,
        "cooldown_seconds": DEFAULT_COOLDOWN,
    }
    await set_state_value(channel_id, _circuit_key(agent_id), data)
    log.debug(
        "Circuit %s: failure #%d (status=%s), encara closed",
        agent_id,
        failures,
        error_status,
    )
    return _to_info(data)


async def reset_circuit(channel_id: str, agent_id: str) -> None:
    """Reset manual: tanca el circuit i neteja els errors."""
    data = _default_circuit()
    await set_state_value(channel_id, _circuit_key(agent_id), data)
    log.info("Circuit %s: reset manual", agent_id)


async def list_circuits(channel_id: str, agent_ids: list[str]) -> list[CircuitInfo]:
    """Llista l'estat del circuit de tots els agents d'un canal."""
    return [await get_circuit(channel_id, aid) for aid in agent_ids]
