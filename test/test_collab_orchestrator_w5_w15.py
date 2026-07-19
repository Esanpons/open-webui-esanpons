"""Tests d'integració W5 + W15 Capa 3 a l'orquestrador.

Cobreix les 9 specs pendents documentades a
docs/tests-w5-w15-capa3-integracio-orchestrator.md:

  CB1–CB5: Circuit breaker a orchestrator (_quick_completion + agent_turn)
  BP1–BP2: Backpressure a orchestrator (_quick_completion + agent_turn)
  D1–D2:   Degradació de context (handraise + agent_turn)

A més a més, cobreix les specs T8–T10 de W11/W12 (effort + tools a agent_turn),
que també depenien de mocks d'orquestrador.
"""

import asyncio
import importlib.metadata
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab import orchestrator
from open_webui.collab.config import CollabConfig
from open_webui.collab.file_tools import COLLAB_TOOL_ID
from open_webui.collab.usage import STATUS_SUCCESS


# ---------------------------------------------------------------------------
# Helpers comuns
# ---------------------------------------------------------------------------


def _setup_quick_completion_mocks(monkeypatch, *, circuit=True, budget_model="a1",
                                 resolved=None, generate_fn=None,
                                 circuit_fn=None):
    """Configura els mocks comuns per _quick_completion."""
    async def circuit_ok(*_a):
        return circuit

    async def resolved_agent(*_a):
        return resolved or {}

    async def budget_ok(*_a, **_kw):
        return budget_model

    async def fake_channel_budget(*_a):
        return None

    async def no_ensure(*_a):
        return False

    async def noop_usage(*_a, **_kw):
        pass

    async def noop_circuit(*_a, **_kw):
        pass

    monkeypatch.setattr(orchestrator, "_circuit_allows", circuit_fn or circuit_ok)
    monkeypatch.setattr(orchestrator, "_resolved_agent", resolved_agent)
    monkeypatch.setattr(orchestrator, "_budget_model_or_none", budget_ok)
    monkeypatch.setattr(orchestrator, "_channel_budget", fake_channel_budget)
    monkeypatch.setattr(orchestrator, "ensure_collab_tool", no_ensure)
    monkeypatch.setattr(orchestrator, "record_usage", noop_usage)
    monkeypatch.setattr(orchestrator, "_record_circuit_result", noop_circuit)

    if generate_fn:
        import open_webui.utils.chat as chat_utils
        monkeypatch.setattr(chat_utils, "generate_chat_completion", generate_fn)


def _setup_agent_turn_mocks(monkeypatch, *, resolved=None, circuit=True,
                            budget_model="a1", degraded=False,
                            capture_form_data=None, capture_project_tree=None,
                            effort_supports=None):
    """Configura els mocks comuns per agent_turn perquè arribi a construir
    form_data i completar la generació sense errors."""

    async def circuit_ok(*_a):
        return circuit

    async def resolved_agent(*_a):
        return resolved or {}

    async def budget_ok(*_a, **_kw):
        return budget_model

    async def fake_is_degraded(*_a):
        return degraded

    async def fake_channel_budget(*_a):
        return {"session_total_tokens": 1} if degraded else None

    async def empty(*_a):
        return ""

    async def execution_phase(*_a):
        return "execution"

    async def new_msg(*_a, **_kw):
        return SimpleNamespace(id="msg-1"), SimpleNamespace(id="c1")

    async def no_ensure(*_a):
        return False

    async def no_proposal(*_a):
        return None

    async def noop(*_a, **_kw):
        pass

    monkeypatch.setattr(orchestrator, "_circuit_allows", circuit_ok)
    monkeypatch.setattr(orchestrator, "_resolved_agent", resolved_agent)
    monkeypatch.setattr(orchestrator, "_budget_model_or_none", budget_ok)
    monkeypatch.setattr(orchestrator, "_is_degraded", fake_is_degraded)
    monkeypatch.setattr(orchestrator, "_channel_budget", fake_channel_budget)
    monkeypatch.setattr(orchestrator, "build_transcript", empty)
    monkeypatch.setattr(orchestrator, "_board_text", empty)
    monkeypatch.setattr(orchestrator, "_current_phase", execution_phase)
    monkeypatch.setattr(orchestrator, "ensure_collab_tool", no_ensure)
    monkeypatch.setattr(orchestrator, "get_end_proposal", no_proposal)
    monkeypatch.setattr(orchestrator, "post_notice", noop)
    monkeypatch.setattr(orchestrator, "_record_circuit_result", noop)
    monkeypatch.setattr(orchestrator, "_mark_agent_up", noop)
    monkeypatch.setattr(orchestrator, "_mark_agent_down", noop)
    monkeypatch.setattr(orchestrator, "record_usage", noop)

    from open_webui.routers import channels as channels_router
    monkeypatch.setattr(channels_router, "new_message_handler", new_msg)

    if capture_project_tree is not None:
        def fake_project_block(config, include_tree=False, *, tree_text=None):
            capture_project_tree["include_tree"] = include_tree
            capture_project_tree["tree_text"] = tree_text
            return ""
        monkeypatch.setattr(orchestrator, "_project_block", fake_project_block)

    if effort_supports is not None:
        monkeypatch.setattr(orchestrator, "_model_supports_effort", lambda _m: effort_supports)

    @asynccontextmanager
    async def fake_acquire(*_a):
        yield

    monkeypatch.setattr(orchestrator, "acquire_model_slot", fake_acquire)

    async def fake_gen(_request, form_data, _user, _message_id):
        if capture_form_data is not None:
            capture_form_data.update(form_data)
        return "ok response"

    monkeypatch.setattr(orchestrator, "_run_generation_until_done", fake_gen)


# ---------------------------------------------------------------------------
# CB1–CB5: Circuit breaker a orchestrator
# ---------------------------------------------------------------------------


def test_cb1_circuit_open_blocks_quick_completion(monkeypatch):
    """CB1 — Circuit obert: _quick_completion retorna None sense cridar el model."""

    async def scenario():
        calls = []

        async def generate_fn(*_a, **_kw):
            calls.append("called")
            return {"choices": [{"message": {"content": "no"}}]}

        _setup_quick_completion_mocks(monkeypatch, circuit=False, generate_fn=generate_fn)
        config = CollabConfig(enabled=True, agents=["a1"])
        result = await orchestrator._quick_completion(
            None, None, SimpleNamespace(id="c1"), config, "a1",
            "system", "prompt", "test",
        )
        assert result is None
        assert calls == []  # no s'ha cridat el model

    asyncio.run(scenario())


def test_cb2_circuit_open_notifies_in_agent_turn(monkeypatch):
    """CB2 — Circuit obert: agent_turn publica avís i retorna None."""

    async def scenario():
        notices = []

        async def notice(*_a, **_kw):
            # post_notice signature: (request, channel, user, content)
            # Però també pot venir com positional
            notices.append("captured")

        async def circuit_blocked(*_a):
            return False

        monkeypatch.setattr(orchestrator, "_circuit_allows", circuit_blocked)
        monkeypatch.setattr(orchestrator, "post_notice", notice)

        config = CollabConfig(enabled=True, agents=["a1"])
        result = await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        assert result is None
        assert len(notices) == 1

    asyncio.run(scenario())


def test_cb3_record_success_after_valid_response(monkeypatch):
    """CB3 — _quick_completion amb resposta vàlida crida _record_circuit_result amb STATUS_SUCCESS."""

    async def scenario():
        circuit_results = []

        async def generate_fn(*_a, **_kw):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        async def capture_circuit(_ch, _ag, status):
            circuit_results.append(status)

        _setup_quick_completion_mocks(monkeypatch, generate_fn=generate_fn)
        monkeypatch.setattr(orchestrator, "_record_circuit_result", capture_circuit)

        config = CollabConfig(enabled=True, agents=["a1"])
        result = await orchestrator._quick_completion(
            None, None, SimpleNamespace(id="c1"), config, "a1",
            "system", "prompt", "test",
        )
        assert result == "ok"
        assert circuit_results == [STATUS_SUCCESS]

    asyncio.run(scenario())


def test_cb4_record_failure_after_error(monkeypatch):
    """CB4 — _quick_completion amb excepció crida _record_circuit_result amb status d'error."""

    async def scenario():
        circuit_results = []

        async def generate_fn(*_a, **_kw):
            raise RuntimeError("model error")

        async def capture_circuit(_ch, _ag, status):
            circuit_results.append(status)

        _setup_quick_completion_mocks(monkeypatch, generate_fn=generate_fn)
        monkeypatch.setattr(orchestrator, "_record_circuit_result", capture_circuit)

        config = CollabConfig(enabled=True, agents=["a1"])
        result = await orchestrator._quick_completion(
            None, None, SimpleNamespace(id="c1"), config, "a1",
            "system", "prompt", "test",
        )
        assert result is None
        assert len(circuit_results) == 1
        assert circuit_results[0] != STATUS_SUCCESS

    asyncio.run(scenario())


def test_transient_rate_limit_is_retried_once(monkeypatch):
    async def scenario():
        calls = 0
        delays = []

        async def generate_fn(*_a, **_kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError(
                    "Rate limit reached. Please try again in 0.01s."
                )
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        async def fake_sleep(delay):
            delays.append(delay)

        _setup_quick_completion_mocks(monkeypatch, generate_fn=generate_fn)
        monkeypatch.setattr(orchestrator.asyncio, "sleep", fake_sleep)

        result = await orchestrator._quick_completion(
            None,
            None,
            SimpleNamespace(id="c1"),
            CollabConfig(enabled=True, agents=["a1"]),
            "a1",
            "system",
            "prompt",
            "handraise",
        )

        assert result == "ok"
        assert calls == 2
        assert delays == [0.01]

    asyncio.run(scenario())


def test_cb5_circuit_fail_open_on_exception(monkeypatch):
    """CB5 — Si can_proceed llança excepció, _circuit_allows retorna True (fail-open)."""

    async def scenario():
        calls = []

        async def generate_fn(*_a, **_kw):
            calls.append("called")
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        # can_proceed és importada de circuit_breaker i _circuit_allows la crida
        async def broken_can_proceed(*_a):
            raise RuntimeError("DB down")

        monkeypatch.setattr(orchestrator, "can_proceed", broken_can_proceed)
        _setup_quick_completion_mocks(monkeypatch, generate_fn=generate_fn)

        config = CollabConfig(enabled=True, agents=["a1"])
        result = await orchestrator._quick_completion(
            None, None, SimpleNamespace(id="c1"), config, "a1",
            "system", "prompt", "test",
        )
        # El circuit ha fet fail-open → la crida ha procedit
        assert result == "ok"
        assert calls == ["called"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# BP1–BP2: Backpressure a orchestrator
# ---------------------------------------------------------------------------


def test_bp1_acquire_model_slot_wraps_quick_completion(monkeypatch):
    """BP1 — acquire_model_slot envolta la crida al model a _quick_completion."""

    async def scenario():
        acquired_models = []

        @asynccontextmanager
        async def tracking_acquire(model_id):
            acquired_models.append(model_id)
            yield

        async def generate_fn(*_a, **_kw):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        monkeypatch.setattr(orchestrator, "acquire_model_slot", tracking_acquire)
        _setup_quick_completion_mocks(monkeypatch, generate_fn=generate_fn)

        config = CollabConfig(enabled=True, agents=["a1"])
        result = await orchestrator._quick_completion(
            None, None, SimpleNamespace(id="c1"), config, "a1",
            "system", "prompt", "test",
        )
        assert result == "ok"
        assert acquired_models == ["a1"]

    asyncio.run(scenario())


def test_bp2_backpressure_wraps_generation_in_agent_turn(monkeypatch):
    """BP2 — _run_with_backpressure envolta _run_generation_until_done amb acquire_model_slot."""

    async def scenario():
        acquired_models = []

        @asynccontextmanager
        async def tracking_acquire(model_id):
            acquired_models.append(model_id)
            yield

        async def fake_gen(_request, _form_data, _user, _message_id):
            return "ok response"

        _setup_agent_turn_mocks(monkeypatch)
        # Sobreescriure per capturar
        monkeypatch.setattr(orchestrator, "acquire_model_slot", tracking_acquire)
        monkeypatch.setattr(orchestrator, "_run_generation_until_done", fake_gen)

        config = CollabConfig(enabled=True, agents=["a1"])
        result = await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        assert result == "ok response"
        assert acquired_models == ["a1"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# D1–D2: Degradació de context (W15 Capa 3)
# ---------------------------------------------------------------------------


def test_d1_handraise_reduces_context_when_degraded(monkeypatch):
    """D1 — handraise() redueix context_messages a 5 quan _is_degraded retorna True."""

    async def scenario():
        async def degraded_true(*_a):
            return True

        async def fake_channel_budget(*_a):
            return {"session_total_tokens": 1}

        captured_config = {}

        async def capture_transcript(_ch, config, _models):
            captured_config["context_messages"] = int(config.guardrail("context_messages") or 30)
            return ""

        async def fake_completion(*_a):
            return '{"intervene": false, "priority": 3, "reason": ""}'

        async def empty(*_a):
            return ""

        async def free_phase(*_a):
            return "free"

        async def no_down(*_a):
            return {}

        async def capture_transition(*_a, **_kw):
            pass

        monkeypatch.setattr(orchestrator, "_is_degraded", degraded_true)
        monkeypatch.setattr(orchestrator, "_channel_budget", fake_channel_budget)
        monkeypatch.setattr(orchestrator, "build_transcript", capture_transcript)
        monkeypatch.setattr(orchestrator, "_quick_completion", fake_completion)
        monkeypatch.setattr(orchestrator, "_board_text", empty)
        monkeypatch.setattr(orchestrator, "_current_phase", free_phase)
        monkeypatch.setattr(orchestrator, "get_down_agents", no_down)
        monkeypatch.setattr(orchestrator, "_transition_receipt", capture_transition)

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.handraise(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, None, 1,
        )
        assert captured_config["context_messages"] == 5

    asyncio.run(scenario())


def test_handraise_uses_short_context_by_default(monkeypatch):
    async def scenario():
        captured_config = {}

        async def not_degraded(*_a):
            return False

        async def no_budget(*_a):
            return None

        async def capture_transcript(_ch, config, _models):
            captured_config["context_messages"] = config.guardrail("context_messages")
            return ""

        async def fake_completion(*_a):
            return '{"intervene": false, "priority": 1, "reason": ""}'

        async def empty(*_a):
            return ""

        async def no_down(*_a):
            return {}

        monkeypatch.setattr(orchestrator, "_is_degraded", not_degraded)
        monkeypatch.setattr(orchestrator, "_channel_budget", no_budget)
        monkeypatch.setattr(orchestrator, "build_transcript", capture_transcript)
        monkeypatch.setattr(orchestrator, "_quick_completion", fake_completion)
        monkeypatch.setattr(orchestrator, "_board_text", empty)
        monkeypatch.setattr(orchestrator, "_current_phase", empty)
        monkeypatch.setattr(orchestrator, "get_down_agents", no_down)

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.handraise(
            None,
            SimpleNamespace(id="c1"),
            config,
            SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}},
            None,
        )

        assert captured_config["context_messages"] == 8

    asyncio.run(scenario())


def test_d2_agent_turn_suppresses_file_tree_when_degraded(monkeypatch):
    """D2 — agent_turn() suprimeix l'arbre de fitxers quan _is_degraded retorna True."""

    async def scenario():
        capture = {}
        _setup_agent_turn_mocks(
            monkeypatch, degraded=True, capture_project_tree=capture,
        )

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        # Degradat → include_tree ha de ser False
        assert capture.get("include_tree") is False

    asyncio.run(scenario())


def test_d2b_agent_turn_includes_file_tree_when_not_degraded(monkeypatch):
    """D2b — agent_turn() inclou l'arbre de fitxers quan no està degradat."""

    async def scenario():
        capture = {}
        _setup_agent_turn_mocks(
            monkeypatch, degraded=False, capture_project_tree=capture,
        )

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        # No degradat → include_tree ha de ser True
        assert capture.get("include_tree") is True

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# T8–T10: Effort i filtratge de tools (W11/W12) a agent_turn
# ---------------------------------------------------------------------------


def test_t8_effort_applied_when_model_supports_it(monkeypatch):
    """T8 — effort s'aplica com a reasoning_effort quan el model el suporta."""

    async def scenario():
        form_data = {}
        _setup_agent_turn_mocks(
            monkeypatch,
            resolved={"effort": "high"},
            effort_supports=True,
            capture_form_data=form_data,
        )

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        assert form_data.get("reasoning_effort") == "high"

    asyncio.run(scenario())


def test_t9_effort_omitted_when_model_does_not_support(monkeypatch):
    """T9 — effort NO s'aplica quan el model no suporta reasoning_effort."""

    async def scenario():
        form_data = {}
        _setup_agent_turn_mocks(
            monkeypatch,
            resolved={"effort": "high"},
            effort_supports=False,
            capture_form_data=form_data,
        )

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        assert "reasoning_effort" not in form_data

    asyncio.run(scenario())


def test_t10_tools_filtered_by_resolved_allowlist(monkeypatch):
    """T10 — tool_ids es filtra segons resolved['tools']: allowlist concreta."""

    async def scenario():
        form_data = {}

        async def fake_ensure(*_a):
            return True

        _setup_agent_turn_mocks(
            monkeypatch,
            resolved={"tools": [COLLAB_TOOL_ID]},
            capture_form_data=form_data,
        )
        # Sobreescriure ensure perquè retorni True (el base retorna False)
        monkeypatch.setattr(orchestrator, "ensure_collab_tool", fake_ensure)

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        # COLLAB_TOOL_ID està a l'allowlist → es manté
        assert COLLAB_TOOL_ID in form_data.get("tool_ids", [])

    asyncio.run(scenario())


def test_t10b_tools_none_keeps_all(monkeypatch):
    """T10b — tools=None (per defecte) permet tots els tools."""

    async def scenario():
        form_data = {}

        async def fake_ensure(*_a):
            return True

        _setup_agent_turn_mocks(
            monkeypatch,
            resolved={},  # sense tools = permet tot
            capture_form_data=form_data,
        )
        monkeypatch.setattr(orchestrator, "ensure_collab_tool", fake_ensure)

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        # sense filter, COLLAB_TOOL_ID es manté
        assert COLLAB_TOOL_ID in form_data.get("tool_ids", [])

    asyncio.run(scenario())


def test_t10c_tools_empty_list_removes_all(monkeypatch):
    """T10c — tools=[] (llista buida) elimina tots els tools."""

    async def scenario():
        form_data = {}

        async def fake_ensure(*_a):
            return True

        _setup_agent_turn_mocks(
            monkeypatch,
            resolved={"tools": []},  # buida = no permet cap
            capture_form_data=form_data,
        )
        monkeypatch.setattr(orchestrator, "ensure_collab_tool", fake_ensure)

        config = CollabConfig(enabled=True, agents=["a1"])
        await orchestrator.agent_turn(
            None, SimpleNamespace(id="c1"), config, SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}}, "a1",
        )
        # allowlist buida → no hi ha tool_ids
        assert form_data.get("tool_ids", []) == []

    asyncio.run(scenario())


def test_tool_calling_unsupported_retries_turn_without_tools(monkeypatch):
    async def scenario():
        forms = []

        async def fake_ensure(*_a):
            return True

        async def fake_generation(_request, form_data, _user, _message_id):
            forms.append(dict(form_data))
            if len(forms) == 1:
                return "Error: `tool calling` is not supported with this model"
            return "resposta sense eines"

        async def reset_message(*_a):
            pass

        _setup_agent_turn_mocks(monkeypatch, resolved={})
        monkeypatch.setattr(orchestrator, "ensure_collab_tool", fake_ensure)
        monkeypatch.setattr(
            orchestrator, "_run_generation_until_done", fake_generation
        )
        monkeypatch.setattr(orchestrator, "_reset_response_for_retry", reset_message)
        orchestrator._models_without_collab_tools.pop("a1", None)

        result = await orchestrator.agent_turn(
            None,
            SimpleNamespace(id="c1"),
            CollabConfig(enabled=True, agents=["a1"]),
            SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}},
            "a1",
        )

        assert result == "resposta sense eines"
        assert COLLAB_TOOL_ID in forms[0].get("tool_ids", [])
        assert "tool_ids" not in forms[1]
        assert "a1" in orchestrator._models_without_collab_tools
        orchestrator._models_without_collab_tools.pop("a1", None)

    asyncio.run(scenario())


def test_empty_turn_with_tools_retries_without_tools(monkeypatch):
    """Un torn buit amb eines adjuntes es reintenta un cop SENSE eines (cas
    Gemini: tool-call silenciosa que deixa el text final buit) però NO es
    memoritza com a model-sense-eines (pot necessitar-les en un altre torn)."""

    async def scenario():
        forms = []

        async def fake_ensure(*_a):
            return True

        async def fake_generation(_request, form_data, _user, _message_id):
            forms.append(dict(form_data))
            if len(forms) == 1:
                return ""  # torn buit AMB eines
            return "Hola! Soc A1."  # reintent sense eines respon bé

        async def reset_message(*_a):
            pass

        _setup_agent_turn_mocks(monkeypatch, resolved={})
        monkeypatch.setattr(orchestrator, "ensure_collab_tool", fake_ensure)
        monkeypatch.setattr(orchestrator, "_run_generation_until_done", fake_generation)
        monkeypatch.setattr(orchestrator, "_reset_response_for_retry", reset_message)
        orchestrator._models_without_collab_tools.pop("a1", None)

        result = await orchestrator.agent_turn(
            None,
            SimpleNamespace(id="c1"),
            CollabConfig(enabled=True, agents=["a1"]),
            SimpleNamespace(id="u1"),
            {"a1": {"name": "A1"}},
            "a1",
        )

        assert result == "Hola! Soc A1."
        assert COLLAB_TOOL_ID in forms[0].get("tool_ids", [])
        assert "tool_ids" not in forms[1]
        # No s'ha de memoritzar: el model sí que admet eines, només va tornar buit.
        assert "a1" not in orchestrator._models_without_collab_tools

    asyncio.run(scenario())
