"""Tests per al mòdul de pressupostos actius (W15 Capa 2).

Cobreix:
- estimate_cost amb preus coneguts i desconeguts.
- check_budget sense pressupost (tot permès).
- check_budget amb per_agent_tokens exhaurit.
- check_budget amb session_total_tokens exhaurit.
- check_budget amb session_total_cost exhaurit.
- Degradació activada al threshold.
- Degradació no activada per sota del threshold.
- Jerarquia: per_agent té prioritat sobre session_total.
- get_session_total_tokens / get_session_total_cost.
"""

import asyncio
import importlib.metadata
from unittest.mock import AsyncMock, patch

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab.budget import (
    ALLOWED_NO_BUDGET,
    ACTIONS_DOWNGRADE,
    ACTIONS_PAUSE,
    ACTIONS_STOP,
    BudgetDecision,
    check_budget,
    estimate_cost,
    get_session_total_cost,
    get_session_total_tokens,
)


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_known_model():
    """gpt-4o: 1000 input + 500 output -> 0.0025 + 0.005 = 0.0075."""
    cost = estimate_cost("gpt-4o", 1000, 500)
    assert abs(cost - 0.0075) < 0.0001


def test_estimate_cost_prefix_match():
    """gpt-4o-mini ha de coincidir per prefix mes llarg, no gpt-4o."""
    cost_full = estimate_cost("gpt-4o-mini", 1000, 1000)
    cost_prefix = estimate_cost("gpt-4o", 1000, 1000)
    assert cost_full != cost_prefix
    # gpt-4o-mini: 0.00015 + 0.0006 = 0.00075
    assert abs(cost_full - 0.00075) < 0.0001


def test_estimate_cost_local_model_zero():
    """Models no llistats (locals com Ollama/qwen) tenen cost 0."""
    assert estimate_cost("qwen2.5:14b", 50000, 50000) == 0.0
    assert estimate_cost("llama3.1:8b", 100000, 100000) == 0.0
    assert estimate_cost("", 1000, 1000) == 0.0


def test_estimate_cost_none_tokens():
    assert estimate_cost("gpt-4o", None, None) == 0.0


# ---------------------------------------------------------------------------
# check_budget -- sense pressupost
# ---------------------------------------------------------------------------


def test_check_budget_no_budget_allows_all():
    """Sense budget (None o buit) tot es permis."""
    async def scenario():
        decision = await check_budget("c1", "a1", "turn", None)
        assert decision == ALLOWED_NO_BUDGET
        decision = await check_budget("c1", "a1", "turn", {})
        assert decision == ALLOWED_NO_BUDGET

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# check_budget -- per_agent_tokens exhaurit
# ---------------------------------------------------------------------------


def test_check_budget_per_agent_exhausted():
    async def scenario():
        with patch(
            "open_webui.collab.budget.get_agent_usage",
            new_callable=AsyncMock,
            return_value={"consumed_tokens": 150_000, "consumed_cost": 0.0, "call_count": 5},
        ), patch(
            "open_webui.collab.budget.get_channel_usage",
            new_callable=AsyncMock,
            return_value={
                "total_tokens": 200_000,
                "total_cost": 1.0,
                "agents": {},
                "call_count": 10,
            },
        ):
            decision = await check_budget(
                "c1", "a1", "turn",
                {"per_agent_tokens": 100_000, "action_on_exhaustion": "pause"},
            )
            assert decision.allowed is False
            assert decision.action == ACTIONS_PAUSE
            assert "agent" in (decision.reason or "").lower()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# check_budget -- session_total_tokens exhaurit
# ---------------------------------------------------------------------------


def test_check_budget_session_tokens_exhausted():
    async def scenario():
        with patch(
            "open_webui.collab.budget.get_agent_usage",
            new_callable=AsyncMock,
            return_value={"consumed_tokens": 50_000, "consumed_cost": 0.0, "call_count": 3},
        ), patch(
            "open_webui.collab.budget.get_channel_usage",
            new_callable=AsyncMock,
            return_value={
                "total_tokens": 600_000,
                "total_cost": 2.0,
                "agents": {},
                "call_count": 12,
            },
        ):
            decision = await check_budget(
                "c1", "a1", "turn",
                {"session_total_tokens": 500_000, "action_on_exhaustion": "stop"},
            )
            assert decision.allowed is False
            assert decision.action == ACTIONS_STOP
            assert "sessi" in (decision.reason or "").lower()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# check_budget -- session_total_cost exhaurit
# ---------------------------------------------------------------------------


def test_check_budget_session_cost_exhausted():
    async def scenario():
        with patch(
            "open_webui.collab.budget.get_agent_usage",
            new_callable=AsyncMock,
            return_value={"consumed_tokens": 50_000, "consumed_cost": 0.5, "call_count": 3},
        ), patch(
            "open_webui.collab.budget.get_channel_usage",
            new_callable=AsyncMock,
            return_value={
                "total_tokens": 200_000,
                "total_cost": 6.5,
                "agents": {},
                "call_count": 12,
            },
        ):
            decision = await check_budget(
                "c1", "a1", "turn",
                {"session_total_cost": 5.0, "action_on_exhaustion": "downgrade"},
            )
            assert decision.allowed is False
            assert decision.action == ACTIONS_DOWNGRADE
            assert "cost" in (decision.reason or "").lower()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# check_budget -- degradacio
# ---------------------------------------------------------------------------


def test_check_budget_degraded_at_threshold():
    """Al 80% del limit per agent, la degradacio s'activa."""
    async def scenario():
        with patch(
            "open_webui.collab.budget.get_agent_usage",
            new_callable=AsyncMock,
            return_value={"consumed_tokens": 85_000, "consumed_cost": 0.0, "call_count": 5},
        ), patch(
            "open_webui.collab.budget.get_channel_usage",
            new_callable=AsyncMock,
            return_value={
                "total_tokens": 200_000,
                "total_cost": 1.0,
                "agents": {},
                "call_count": 10,
            },
        ):
            decision = await check_budget(
                "c1", "a1", "turn",
                {"per_agent_tokens": 100_000},
            )
            assert decision.allowed is True
            assert decision.degraded is True

    asyncio.run(scenario())


def test_check_budget_not_degraded_below_threshold():
    """Per sota del threshold, sense degradacio."""
    async def scenario():
        with patch(
            "open_webui.collab.budget.get_agent_usage",
            new_callable=AsyncMock,
            return_value={"consumed_tokens": 50_000, "consumed_cost": 0.0, "call_count": 3},
        ), patch(
            "open_webui.collab.budget.get_channel_usage",
            new_callable=AsyncMock,
            return_value={
                "total_tokens": 100_000,
                "total_cost": 0.0,
                "agents": {},
                "call_count": 6,
            },
        ):
            decision = await check_budget(
                "c1", "a1", "turn",
                {"per_agent_tokens": 100_000},
            )
            assert decision.allowed is True
            assert decision.degraded is False

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# check_budget -- jerarquia de comprovacio
# ---------------------------------------------------------------------------


def test_check_budget_per_agent_takes_precedence_over_session():
    """Si per_agent esta exhaurit, el missatge diu 'agent', no 'sessio'."""
    async def scenario():
        with patch(
            "open_webui.collab.budget.get_agent_usage",
            new_callable=AsyncMock,
            return_value={"consumed_tokens": 120_000, "consumed_cost": 0.0, "call_count": 5},
        ), patch(
            "open_webui.collab.budget.get_channel_usage",
            new_callable=AsyncMock,
            return_value={
                "total_tokens": 700_000,
                "total_cost": 3.0,
                "agents": {},
                "call_count": 15,
            },
        ):
            decision = await check_budget(
                "c1", "a1", "turn",
                {
                    "per_agent_tokens": 100_000,
                    "session_total_tokens": 500_000,
                },
            )
            assert decision.allowed is False
            assert "agent" in (decision.reason or "").lower()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# get_session_total_tokens / get_session_total_cost
# ---------------------------------------------------------------------------


def test_get_session_total_tokens():
    async def scenario():
        with patch(
            "open_webui.collab.budget.get_channel_usage",
            new_callable=AsyncMock,
            return_value={
                "total_tokens": 42_000,
                "total_cost": 1.5,
                "agents": {},
                "call_count": 8,
            },
        ):
            total = await get_session_total_tokens("c1")
            assert total == 42_000

    asyncio.run(scenario())


def test_get_session_total_cost():
    async def scenario():
        with patch(
            "open_webui.collab.budget.get_channel_usage",
            new_callable=AsyncMock,
            return_value={
                "total_tokens": 42_000,
                "total_cost": 1.5,
                "agents": {},
                "call_count": 8,
            },
        ):
            cost = await get_session_total_cost("c1")
            assert cost == 1.5

    asyncio.run(scenario())
