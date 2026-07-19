"""Telemetria de consum de l'espai col·laboratiu — W15 Capa 1 (Fase 0).

Registra cada crida a un model (hand-raise, torn, vot, resum) al log
``collab_usage`` i manté l'agregat per (canal, agent) a
``collab_budget_tracker``, de manera que la comprovació de pressupostos
(Capa 2, vegeu docs/disseny-w15-capa2-pressupostos.md) sigui O(1) i mai
un SUM sobre el log.

Concurrència: log i agregat s'escriuen en LA MATEIXA transacció, i l'upsert
de l'agregat és la PRIMERA sentència — en SQLite la connexió adquireix el
write-lock d'entrada, sense upgrade de lock a mitja transacció (el problema
que el «BEGIN IMMEDIATE» del disseny volia evitar; amb WAL + busy_timeout la
contenció es resol esperant). En PostgreSQL, ON CONFLICT és atòmic per si sol.

``error_detail`` es guarda SEMPRE sanejat: mai prompts, respostes completes
ni credencials (vegeu sanitize_error_detail).
"""

import asyncio
import re
import time
import uuid

from open_webui.internal.db import Base, get_async_db_context
from sqlalchemy import BigInteger, Column, Float, Integer, Text, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

############################
# Categories d'estat (6 categories d'error + èxit — mai text lliure)
############################

STATUS_SUCCESS = "success"
STATUS_QUOTA = "quota_exceeded"
STATUS_CONTEXT = "context_too_large"
STATUS_TIMEOUT = "timeout"
STATUS_PROVIDER = "provider_error"
STATUS_EMPTY = "empty_response"
STATUS_CLI = "cli_error"

ERROR_STATUSES = (
    STATUS_QUOTA,
    STATUS_CONTEXT,
    STATUS_TIMEOUT,
    STATUS_PROVIDER,
    STATUS_EMPTY,
    STATUS_CLI,
)

CALL_TYPES = ("handraise", "turn", "vote", "summary")

############################
# Models
############################


class CollabUsage(Base):
    __tablename__ = "collab_usage"

    id = Column(Text, primary_key=True)
    channel_id = Column(Text, nullable=False)
    agent_id = Column(Text, nullable=False)
    call_type = Column(Text, nullable=False)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    estimated_cost = Column(Float, nullable=True)
    status = Column(Text, nullable=False, default=STATUS_SUCCESS)
    error_detail = Column(Text, nullable=True)
    created_at = Column(BigInteger, nullable=False)


class CollabBudgetTracker(Base):
    __tablename__ = "collab_budget_tracker"

    channel_id = Column(Text, primary_key=True)
    agent_id = Column(Text, primary_key=True)
    consumed_tokens = Column(BigInteger, nullable=False, default=0)
    consumed_cost = Column(Float, nullable=False, default=0.0)
    call_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(BigInteger, nullable=False)


############################
# Classificació d'errors
############################

# L'ordre importa: la primera coincidència guanya. Quota abans que timeout
# (molts errors de quota mencionen "retry"), i CLI abans que provider (un
# error embolcallat pel pipe CLI és cli_error encara que dins hi hagi un 500).
_QUOTA_RE = re.compile(
    r"\b429\b|rate.?limit|quota|usage limit|too many requests|insufficient_quota",
    re.IGNORECASE,
)
_CONTEXT_RE = re.compile(
    r"\b413\b|context.?(?:length|window)|context[_ ]too[_ ]large|maximum context"
    r"|prompt is too long|payload too large|input is too long",
    re.IGNORECASE,
)
_TIMEOUT_RE = re.compile(r"timed?.?out|timeout", re.IGNORECASE)
_CLI_RE = re.compile(
    r"\*\*(?:claude|codex)[^\n]{0,40}error|no ha retornat resposta",
    re.IGNORECASE,
)
_PROVIDER_RE = re.compile(
    r"connection (?:refused|reset|error)|\b5\d{2}\b|service unavailable"
    r"|internal server error|bad gateway|dns|ssl",
    re.IGNORECASE,
)

_CLASSIFICATION = (
    (_QUOTA_RE, STATUS_QUOTA),
    (_CONTEXT_RE, STATUS_CONTEXT),
    (_TIMEOUT_RE, STATUS_TIMEOUT),
    (_CLI_RE, STATUS_CLI),
    (_PROVIDER_RE, STATUS_PROVIDER),
)

# Sanejament d'error_detail: mai credencials, mai text il·limitat.
_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(?:api[_-]?key|token|authorization|secret)\s*[=:]\s*\S+"),
)
MAX_ERROR_DETAIL = 300


def sanitize_error_detail(detail) -> str | None:
    """Compacta espais, redacta credencials i limita la longitud.

    El que entra aquí ha de ser un missatge d'error, mai un prompt ni una
    resposta completa — aquest límit és l'última xarxa, no la política.
    """
    if not detail:
        return None
    out = " ".join(str(detail).split())
    for pattern in _SECRET_RES:
        out = pattern.sub("[redacted]", out)
    return out[:MAX_ERROR_DETAIL] or None


def classify_error(raw) -> tuple[str, str | None]:
    """Classifica un error (excepció o text) en una de les 6 categories.

    Aquest és el classificador CENTRAL de tots els errors de model del mòdul
    collab. Les regex heurístiques de `orchestrator.py` (`_TOKEN_LIMIT_HINT_RE`,
    `_TOOL_CALLING_UNSUPPORTED_RE`, `_RETRY_AFTER_RE`) governen decisions de
    RETRY, no la classificació de status; qualsevol categoria d'estat nova ha
    d'anar aquí. El contracte està cobert per test_collab_error_classification.

    Retorna (status, error_detail_sanejat). Un `raw` buit és empty_response.
    Desconegut → provider_error (mai text lliure com a status).
    """
    if isinstance(raw, BaseException):
        if isinstance(raw, (asyncio.TimeoutError, TimeoutError)):
            return STATUS_TIMEOUT, sanitize_error_detail(f"{type(raw).__name__}: {raw}")
        message = f"{type(raw).__name__}: {raw}"
    else:
        message = str(raw or "")

    if not message.strip():
        return STATUS_EMPTY, None

    for pattern, status in _CLASSIFICATION:
        if pattern.search(message):
            return status, sanitize_error_detail(message)
    return STATUS_PROVIDER, sanitize_error_detail(message)


def estimate_tokens(text: str | None) -> int:
    """Estimació barata (~4 caràcters/token) quan el proveïdor no retorna usage."""
    if not text:
        return 0
    return max(1, len(text) // 4)


############################
# Registre (log + agregat, una sola transacció)
############################


async def record_usage(
    channel_id: str,
    agent_id: str,
    call_type: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    estimated_cost: float | None = None,
    status: str = STATUS_SUCCESS,
    error_detail: str | None = None,
    db=None,
) -> None:
    """Registra una crida al log i actualitza l'agregat atòmicament."""
    now = int(time.time())
    total_tokens = (
        None
        if input_tokens is None and output_tokens is None
        else (input_tokens or 0) + (output_tokens or 0)
    )
    async with get_async_db_context(db) as session:
        dialect = session.get_bind().dialect.name
        insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
        upsert = (
            insert_fn(CollabBudgetTracker)
            .values(
                channel_id=channel_id,
                agent_id=agent_id,
                consumed_tokens=total_tokens or 0,
                consumed_cost=float(estimated_cost or 0.0),
                call_count=1,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["channel_id", "agent_id"],
                set_={
                    "consumed_tokens": CollabBudgetTracker.consumed_tokens
                    + (total_tokens or 0),
                    "consumed_cost": CollabBudgetTracker.consumed_cost
                    + float(estimated_cost or 0.0),
                    "call_count": CollabBudgetTracker.call_count + 1,
                    "updated_at": now,
                },
            )
        )
        # Primer l'upsert (escriptura): en SQLite fixa el write-lock d'entrada.
        await session.execute(upsert)
        session.add(
            CollabUsage(
                id=str(uuid.uuid4()),
                channel_id=channel_id,
                agent_id=agent_id,
                call_type=call_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                estimated_cost=estimated_cost,
                status=status,
                error_detail=sanitize_error_detail(error_detail),
                created_at=now,
            )
        )
        await session.commit()


############################
# Consulta (sempre sobre l'agregat — O(nombre d'agents), mai sobre el log)
############################


async def get_agent_usage(channel_id: str, agent_id: str, db=None) -> dict:
    async with get_async_db_context(db) as session:
        result = await session.execute(
            select(CollabBudgetTracker).filter(
                CollabBudgetTracker.channel_id == channel_id,
                CollabBudgetTracker.agent_id == agent_id,
            )
        )
        row = result.scalars().first()
        if not row:
            return {"consumed_tokens": 0, "consumed_cost": 0.0, "call_count": 0}
        return {
            "consumed_tokens": row.consumed_tokens,
            "consumed_cost": row.consumed_cost,
            "call_count": row.call_count,
        }


async def get_channel_usage(channel_id: str, db=None) -> dict:
    """Totals de sessió + desglossament per agent (per als comptadors de W1/W15)."""
    async with get_async_db_context(db) as session:
        result = await session.execute(
            select(CollabBudgetTracker).filter(
                CollabBudgetTracker.channel_id == channel_id
            )
        )
        rows = result.scalars().all()
    agents = {
        row.agent_id: {
            "consumed_tokens": row.consumed_tokens,
            "consumed_cost": row.consumed_cost,
            "call_count": row.call_count,
        }
        for row in rows
    }
    return {
        "agents": agents,
        "total_tokens": sum(a["consumed_tokens"] for a in agents.values()),
        "total_cost": sum(a["consumed_cost"] for a in agents.values()),
        "call_count": sum(a["call_count"] for a in agents.values()),
    }
