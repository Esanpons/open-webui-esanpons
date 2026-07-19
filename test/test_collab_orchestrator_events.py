import asyncio
import importlib.metadata
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab import orchestrator
from open_webui.collab import router as collab_router
from open_webui.collab.config import CollabConfig


def test_emit_collab_event_uses_channel_envelope(monkeypatch):
    emitted = []

    async def fake_emit(name, payload, *, to):
        emitted.append((name, payload, to))

    monkeypatch.setattr(orchestrator.sio, "emit", fake_emit)
    event = SimpleNamespace(
        channel_id="c1",
        message_id="m1",
        seq=7,
        type="agent_state",
        agent_id="a1",
        payload={"state": "evaluating"},
        status="active",
        created_at=123,
    )

    asyncio.run(orchestrator._emit_collab_event(event))

    name, payload, room = emitted[0]
    assert name == "events:channel"
    assert room == "channel:c1"
    assert payload["data"] == {
        "type": "collab_event.v1",
        "data": {
            "seq": 7,
            "event": {
                "type": "agent_state",
                "agent_id": "a1",
                "message_id": "m1",
                "payload": {"state": "evaluating"},
                "status": "active",
                "timestamp": 123,
            },
        },
    }


def test_handraise_orders_by_priority_and_configuration(monkeypatch):
    async def scenario():
        async def fake_completion(_request, _user, _channel, _config, agent_id, *_args, **_kwargs):
            priority = {"a1": 5, "a2": 2}[agent_id]
            return f'{{"intervene": true, "priority": {priority}, "reason": "ok"}}'

        async def empty_text(*_args):
            return ""

        async def free_phase(*_args):
            return "free"

        async def no_down(*_args):
            return {}

        transitions = []

        async def capture_transition(_channel_id, _seq, agent_id, state):
            transitions.append((agent_id, state))

        monkeypatch.setattr(orchestrator, "_quick_completion", fake_completion)
        monkeypatch.setattr(orchestrator, "build_transcript", empty_text)
        monkeypatch.setattr(orchestrator, "_board_text", empty_text)
        monkeypatch.setattr(orchestrator, "_current_phase", free_phase)
        monkeypatch.setattr(orchestrator, "get_down_agents", no_down)
        monkeypatch.setattr(orchestrator, "_transition_receipt", capture_transition)

        config = CollabConfig(enabled=True, agents=["a1", "a2"])
        volunteers, responded, asked = await orchestrator.handraise(
            None,
            SimpleNamespace(id="c1"),
            config,
            None,
            {"a1": {"name": "A1"}, "a2": {"name": "A2"}},
            None,
            1,
        )
        assert volunteers == ["a1", "a2"]
        assert (responded, asked) == (2, 2)
        assert transitions == [
            ("a1", "evaluating"),
            ("a2", "evaluating"),
            ("a1", "will_intervene"),
            ("a2", "will_intervene"),
        ]

    asyncio.run(scenario())


def test_handraise_skips_last_speaker_and_closes_receipt(monkeypatch):
    async def scenario():
        calls = []

        async def fake_completion(_request, _user, _channel, _config, agent_id, *_args, **_kwargs):
            calls.append(agent_id)
            return '{"intervene": false, "priority": 3, "reason": ""}'

        async def empty_text(*_args):
            return ""

        async def free_phase(*_args):
            return "free"

        async def no_down(*_args):
            return {}

        transitions = []

        async def capture_transition(_channel_id, _seq, agent_id, state):
            transitions.append((agent_id, state))

        monkeypatch.setattr(orchestrator, "_quick_completion", fake_completion)
        monkeypatch.setattr(orchestrator, "build_transcript", empty_text)
        monkeypatch.setattr(orchestrator, "_board_text", empty_text)
        monkeypatch.setattr(orchestrator, "_current_phase", free_phase)
        monkeypatch.setattr(orchestrator, "get_down_agents", no_down)
        monkeypatch.setattr(orchestrator, "_transition_receipt", capture_transition)

        config = CollabConfig(
            enabled=True,
            agents=["a1", "a2"],
            guardrails={"allow_self_reply": False},
        )
        volunteers, responded, asked = await orchestrator.handraise(
            None,
            SimpleNamespace(id="c1"),
            config,
            None,
            {"a1": {"name": "A1"}, "a2": {"name": "A2"}},
            "a1",
            1,
        )
        assert volunteers == []
        assert calls == ["a2"]
        assert (responded, asked) == (1, 1)
        assert transitions == [
            ("a1", "pass"),
            ("a2", "evaluating"),
            ("a2", "pass"),
        ]

    asyncio.run(scenario())


def test_roundrobin_runs_each_agent_once_and_releases_lease(monkeypatch):
    async def scenario():
        channel = SimpleNamespace(id="c1")
        config = CollabConfig(enabled=True, agents=["a1", "a2", "a3"], mode="roundrobin")
        turns = []
        transitions = []
        releases = []

        async def acquire(*_args, **_kwargs):
            return True

        async def release(channel_id, owner, *, stopped=False):
            releases.append((channel_id, owner, stopped))
            return True

        async def hold_lease(*_args):
            await asyncio.Event().wait()

        async def get_channel(_channel_id):
            return channel

        async def no_op(*_args, **_kwargs):
            return None

        async def no_down(*_args):
            return {}

        async def fake_models(*_args):
            return {agent: {"name": agent.upper()} for agent in config.agents}

        async def fake_turn(_request, _channel, _config, _user, _models, speaker, **_kwargs):
            turns.append(speaker)
            return f"turn {speaker}"

        async def capture_transition(_channel_id, _seq, agent_id, state):
            transitions.append((agent_id, state))

        monkeypatch.setattr(orchestrator, "acquire_lease", acquire)
        monkeypatch.setattr(orchestrator, "release_lease", release)
        monkeypatch.setattr(orchestrator, "_renew_round_lease", hold_lease)
        monkeypatch.setattr(orchestrator, "cleanup_orphan_turn_messages", no_op)
        monkeypatch.setattr(orchestrator.Channels, "get_channel_by_id", get_channel)
        monkeypatch.setattr(orchestrator, "get_collab_config", lambda _channel: config)
        monkeypatch.setattr(orchestrator, "clear_end_proposal", no_op)
        monkeypatch.setattr(orchestrator, "get_end_proposal", no_op)
        monkeypatch.setattr(orchestrator, "get_down_agents", no_down)
        monkeypatch.setattr(orchestrator, "_get_models", fake_models)
        monkeypatch.setattr(orchestrator, "agent_turn", fake_turn)
        monkeypatch.setattr(orchestrator, "post_notice", no_op)
        monkeypatch.setattr(orchestrator, "_transition_receipt", capture_transition)

        await orchestrator.run_round(None, channel, None, event_seq=1)

        assert turns == ["a1", "a2", "a3"]
        # Cada agent: will_intervene (abans del torn) i incorporated (després,
        # ha respost incorporant el context del missatge humà — MR-26).
        assert transitions == [
            ("a1", "will_intervene"),
            ("a1", "incorporated"),
            ("a2", "will_intervene"),
            ("a2", "incorporated"),
            ("a3", "will_intervene"),
            ("a3", "incorporated"),
        ]
        assert len(releases) == 1
        assert releases[0][0] == "c1"
        assert releases[0][2] is False
        assert "c1" not in orchestrator._active_rounds

    asyncio.run(scenario())


def test_continuous_mode_restarts_queue_for_new_user_event_between_turns(monkeypatch):
    async def scenario():
        channel = SimpleNamespace(id="c1")
        config = CollabConfig(
            enabled=True,
            agents=["a1", "a2"],
            mode="roundrobin",
            conversation_mode="continuous",
        )
        turns = []
        transitions = []
        new_message_available = False

        async def acquire(*_args, **_kwargs):
            return True

        async def release(*_args, **_kwargs):
            return True

        async def hold_lease(*_args):
            await asyncio.Event().wait()

        async def get_channel(_channel_id):
            return channel

        async def no_op(*_args, **_kwargs):
            return None

        async def no_down(*_args):
            return {}

        async def fake_models(*_args):
            return {agent: {"name": agent.upper()} for agent in config.agents}

        async def fake_turn(_request, _channel, _config, _user, _models, speaker, **_kwargs):
            nonlocal new_message_available
            turns.append(speaker)
            if len(turns) == 1:
                new_message_available = True
            return f"turn {speaker}"

        async def events_after(_channel_id, *, since=0, limit=200, db=None):
            if new_message_available and since < 2:
                return [SimpleNamespace(seq=2, type="user_message")]
            return []

        async def capture_transition(_channel_id, seq, agent_id, state):
            transitions.append((seq, agent_id, state))

        monkeypatch.setattr(orchestrator, "acquire_lease", acquire)
        monkeypatch.setattr(orchestrator, "release_lease", release)
        monkeypatch.setattr(orchestrator, "_renew_round_lease", hold_lease)
        monkeypatch.setattr(orchestrator, "cleanup_orphan_turn_messages", no_op)
        monkeypatch.setattr(orchestrator.Channels, "get_channel_by_id", get_channel)
        monkeypatch.setattr(orchestrator, "get_collab_config", lambda _channel: config)
        monkeypatch.setattr(orchestrator, "clear_end_proposal", no_op)
        monkeypatch.setattr(orchestrator, "get_end_proposal", no_op)
        monkeypatch.setattr(orchestrator, "get_down_agents", no_down)
        monkeypatch.setattr(orchestrator, "_get_models", fake_models)
        monkeypatch.setattr(orchestrator, "agent_turn", fake_turn)
        monkeypatch.setattr(orchestrator, "post_notice", no_op)
        monkeypatch.setattr(orchestrator, "list_events", events_after)
        monkeypatch.setattr(orchestrator, "_transition_receipt", capture_transition)

        await orchestrator.run_round(None, channel, None, event_seq=1)

        assert turns == ["a1", "a1", "a2"]
        # will_intervene abans de cada torn + incorporated després (MR-26).
        # El seq salta d'1 a 2 quan arriba el missatge humà nou entre torns.
        assert transitions == [
            (1, "a1", "will_intervene"),
            (1, "a1", "incorporated"),
            (2, "a1", "will_intervene"),
            (2, "a1", "incorporated"),
            (2, "a2", "will_intervene"),
            (2, "a2", "incorporated"),
        ]

    asyncio.run(scenario())


def test_renew_round_lease_marks_clean_lease_loss(monkeypatch):
    async def scenario():
        state = {"stop": False, "lease_lost": False}

        async def immediate_sleep(_seconds):
            return None

        async def lost(*_args, **_kwargs):
            return False

        monkeypatch.setattr(orchestrator.asyncio, "sleep", immediate_sleep)
        monkeypatch.setattr(orchestrator, "renew_lease", lost)

        await orchestrator._renew_round_lease("c1", "worker-1", state)

        assert state == {"stop": True, "lease_lost": True}

    asyncio.run(scenario())


def test_cancel_turn_cancels_only_matching_channel():
    async def scenario():
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(slow())
        await started.wait()
        orchestrator._turn_cancellables["t1"] = {
            "channel_id": "c1",
            "task": task,
            "cancel_reason": None,
        }
        try:
            assert await orchestrator.cancel_turn("c2", "t1") is False
            assert task.cancelled() is False
            assert await orchestrator.cancel_turn("c1", "t1") is True
            with pytest.raises(asyncio.CancelledError):
                await task
            assert orchestrator._turn_cancellables["t1"]["cancel_reason"] == "user_requested"
        finally:
            orchestrator._turn_cancellables.pop("t1", None)
            if not task.done():
                task.cancel()

    asyncio.run(scenario())


def test_cancel_turn_waits_for_effectful_tool_unlock():
    async def scenario():
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(slow())
        await started.wait()
        orchestrator._turn_cancellables["locked-turn"] = {
            "channel_id": "c1",
            "task": task,
            "cancel_reason": None,
            "cancel_pending": False,
            "tool_lock_depth": 0,
        }
        try:
            assert orchestrator.lock_turn_tool("locked-turn", "write_project_file")
            assert await orchestrator.cancel_turn("c1", "locked-turn") is False
            assert task.cancelled() is False
            assert orchestrator._turn_cancellables["locked-turn"]["cancel_pending"]
            assert orchestrator.unlock_turn_tool("locked-turn") is True
            with pytest.raises(asyncio.CancelledError):
                await task
            assert orchestrator._turn_cancellables["locked-turn"]["active_tool"] is None
        finally:
            orchestrator._turn_cancellables.pop("locked-turn", None)
            if not task.done():
                task.cancel()

    asyncio.run(scenario())


def test_turn_timeout_zero_disables_limit():
    assert orchestrator._effective_turn_timeout(
        CollabConfig(guardrails={"turn_timeout": 0})
    ) is None


def test_budget_gate_downgrades_or_pauses_and_stops(monkeypatch):
    async def scenario():
        notices = []
        stopped = []

        async def channel_budget(_channel_id):
            return {"session_total_tokens": 1}

        async def notice(_request, _channel, _user, message):
            notices.append(message)

        monkeypatch.setattr(orchestrator, "_channel_budget", channel_budget)
        monkeypatch.setattr(orchestrator, "post_notice", notice)
        monkeypatch.setattr(orchestrator, "request_stop", stopped.append)
        channel = SimpleNamespace(id="budget-channel")

        async def exhausted(_channel_id, _agent_id, _call_type, _budget):
            return SimpleNamespace(
                allowed=False,
                degraded=False,
                reason="límit",
                action="downgrade",
            )

        monkeypatch.setattr(orchestrator, "check_budget", exhausted)
        assert await orchestrator._budget_model_or_none(
            None,
            channel,
            None,
            "agent-a",
            "turn",
            {"fallback_model_id": "cheap-model"},
        ) == "cheap-model"
        assert notices == []

        assert await orchestrator._budget_model_or_none(
            None, channel, None, "agent-b", "turn", {"fallback_model_id": None}
        ) is None
        assert notices == ["⏸️ límit."]

        async def stopped_budget(*_args):
            return SimpleNamespace(
                allowed=False,
                degraded=False,
                reason="aturat",
                action="stop",
            )

        monkeypatch.setattr(orchestrator, "check_budget", stopped_budget)
        assert await orchestrator._budget_model_or_none(
            None, channel, None, "agent-a", "vote", {}
        ) is None
        assert stopped == ["budget-channel"]
        assert notices[-1] == "🛑 aturat."

    asyncio.run(scenario())


def test_effective_config_ignores_stale_channel_config(monkeypatch):
    async def scenario():
        async def channel_config(_channel_id):
            return {
                "config": {
                    "enabled": False,
                    "agents": [],
                    "mode": "roundrobin",
                }
            }

        monkeypatch.setattr(orchestrator, "get_channel_config", channel_config)
        channel = SimpleNamespace(
            id="c1",
            meta={
                "collab": {
                    "enabled": True,
                    "agents": ["a", "b"],
                    "project_dir": "D:/project",
                    "mode": "handraise",
                    "conversation_mode": "continuous",
                    "guardrails": {"context_messages": 30},
                }
            },
        )
        config = await orchestrator._effective_collab_config(channel)
        assert config.enabled is True
        assert config.agents == ["a", "b"]
        assert config.project_dir == "D:/project"
        assert config.mode == "handraise"
        assert config.conversation_mode == "continuous"
        assert config.guardrails == {"context_messages": 30}

    asyncio.run(scenario())
    assert orchestrator._effective_turn_timeout(
        CollabConfig(guardrails={"turn_timeout": 30})
    ) == 30
    assert orchestrator._effective_turn_timeout(
        CollabConfig(guardrails={"turn_timeout": 900})
    ) == 900


def test_cancel_turn_endpoint_reports_result(monkeypatch):
    async def scenario():
        async def allow(*_args, **_kwargs):
            return SimpleNamespace(id="c1")

        calls = []

        async def cancel(channel_id, turn_id=None, reason="user_requested"):
            calls.append((channel_id, turn_id, reason))
            return True

        monkeypatch.setattr(collab_router, "_check_can_manage", lambda _user: None)
        monkeypatch.setattr(collab_router, "_get_channel_checked", allow)
        monkeypatch.setattr(collab_router, "cancel_turn", cancel)
        response = await collab_router.cancel_channel_turn(
            SimpleNamespace(), "c1", user=SimpleNamespace()
        )
        assert response == {"cancelled": True}
        assert calls == [("c1", None, "user_requested")]

    asyncio.run(scenario())


def test_reconcile_channel_only_touches_nonlocal_session(monkeypatch):
    async def scenario():
        calls = []

        async def reconcile(channel_id):
            calls.append(channel_id)
            return True

        monkeypatch.setattr(orchestrator, "reconcile_expired_session", reconcile)
        orchestrator._active_rounds["local"] = {"stop": False}
        try:
            assert await orchestrator.reconcile_channel("local") is False
            assert await orchestrator.reconcile_channel("stale") is True
            assert calls == ["stale"]
        finally:
            orchestrator._active_rounds.pop("local", None)

    asyncio.run(scenario())


def test_events_endpoint_formats_incremental_events(monkeypatch):
    async def scenario():
        async def allow(*_args, **_kwargs):
            return SimpleNamespace(id="c1")

        async def fake_events(channel_id, *, since, limit):
            assert (channel_id, since, limit) == ("c1", 4, 10)
            return [
                SimpleNamespace(
                    id="e5",
                    seq=5,
                    type="agent_state",
                    agent_id="a1",
                    message_id="m1",
                    payload={"state": "pass"},
                    status="active",
                    created_at=123,
                )
            ]

        monkeypatch.setattr(collab_router, "_get_channel_checked", allow)
        monkeypatch.setattr(collab_router, "list_events", fake_events)
        response = await collab_router.get_collab_events(
            SimpleNamespace(), "c1", since=4, limit=10, user=SimpleNamespace()
        )
        assert response == {
            "events": [
                {
                    "id": "e5",
                    "seq": 5,
                    "type": "agent_state",
                    "agent_id": "a1",
                    "message_id": "m1",
                    "payload": {"state": "pass"},
                    "status": "active",
                    "created_at": 123,
                }
            ]
        }

    asyncio.run(scenario())


def test_receipts_endpoint_formats_summary_and_propagates_access_denial(monkeypatch):
    async def scenario():
        async def allow(*_args, **_kwargs):
            return SimpleNamespace(id="c1")

        async def fake_receipts(_channel_id, _event_seq):
            return [
                SimpleNamespace(
                    agent_id="a1", state="evaluating", message_id="m1", updated_at=99
                )
            ]

        async def fake_summary(_channel_id, _event_seq):
            return {"evaluating": 1, "total": 1}

        monkeypatch.setattr(collab_router, "_get_channel_checked", allow)
        monkeypatch.setattr(collab_router, "list_receipts", fake_receipts)
        monkeypatch.setattr(collab_router, "receipt_summary", fake_summary)
        response = await collab_router.get_collab_receipts(
            SimpleNamespace(), "c1", 1, user=SimpleNamespace()
        )
        assert response == {
            "event_seq": 1,
            "receipts": [
                {
                    "agent_id": "a1",
                    "state": "evaluating",
                    "message_id": "m1",
                    "updated_at": 99,
                }
            ],
            "summary": {"evaluating": 1, "total": 1},
        }

        async def deny(*_args, **_kwargs):
            raise HTTPException(status_code=403, detail="Sense accés al canal")

        monkeypatch.setattr(collab_router, "_get_channel_checked", deny)
        with pytest.raises(HTTPException) as error:
            await collab_router.get_collab_events(
                SimpleNamespace(), "c1", since=0, limit=10, user=SimpleNamespace()
            )
        assert error.value.status_code == 403

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# T1–T12: Specs W11/W12 (resolució d'agent + personalització)
# B5–B8: Specs W15 Capa 2 (pressupostos — casos addicionals)
# ---------------------------------------------------------------------------


def test_t1_resolved_agent_no_overrides_returns_base_values(monkeypatch):
    """T1 — resolve_agent sense overrides retorna valors base."""

    async def scenario():
        async def channel_config(_channel_id):
            return {}

        monkeypatch.setattr(orchestrator, "get_channel_config", channel_config)
        result = await orchestrator._resolved_agent("c1", "agent-a")
        assert result.get("priority") == 3
        # sense overrides, el model base és el propi agent (els agents SÓN model_ids)
        assert result.get("model_id") == "agent-a"
        assert result.get("system_prompt") is None
        assert result.get("role") is None

    asyncio.run(scenario())


def test_t2_resolved_agent_override_matched_by_model_id(monkeypatch):
    """T2 — l'override s'identifica per model_id i aplica els seus camps.

    Nota de disseny: l'override no substitueix el model (el model_id és la
    clau d'identificació); per canviar de model hi ha fallback_model_id.
    """

    async def scenario():
        async def channel_config(_channel_id):
            return {
                "agent_overrides": [
                    {"model_id": "agent-a", "role": "Revisor", "fallback_model_id": "gpt-4o-mini"},
                    {"model_id": "agent-b", "role": "Ignorat"},
                ]
            }

        monkeypatch.setattr(orchestrator, "get_channel_config", channel_config)
        result = await orchestrator._resolved_agent("c1", "agent-a")
        assert result.get("model_id") == "agent-a"
        assert result.get("role") == "Revisor"
        assert result.get("fallback_model_id") == "gpt-4o-mini"

    asyncio.run(scenario())


def test_t3_apply_agent_prompt_prepends_role_and_system_prompt():
    """T3 — _apply_agent_prompt prependeix rol + system_prompt."""
    resolved = {"role": "Tester", "system_prompt": "Comprova tot abans de lliurar."}
    result = orchestrator._apply_agent_prompt("Ets un agent...", resolved, "Agent A")
    assert result.startswith("Funció específica: Tester.")
    assert "Instruccions específiques: Comprova tot abans de lliurar." in result
    assert result.endswith("\n\nEts un agent...")


def test_t4_apply_agent_prompt_without_overrides_returns_original():
    """T4 — _apply_agent_prompt sense overrides retorna el system original."""
    resolved = {}
    result = orchestrator._apply_agent_prompt("Ets un agent...", resolved, "Agent A")
    assert result == "Ets un agent..."


def test_t5_model_supports_effort_detects_capability():
    """T5 — _model_supports_effort detecta effort als capabilities."""
    model = {"capabilities": {"reasoning_effort": True}}
    assert orchestrator._model_supports_effort(model) is True


def test_t6_model_supports_effort_false_without_capabilities():
    """T6 — _model_supports_effort retorna False sense capabilities."""
    assert orchestrator._model_supports_effort({}) is False


def test_t7_token_limit_applied_as_max_tokens(monkeypatch):
    """T7 — token_limit s'aplica com a max_tokens al form_data de _quick_completion."""

    async def scenario():
        captured_form_data = {}

        async def channel_config(_channel_id):
            return {
                "agent_overrides": [
                    {"model_id": "agent-a", "token_limit": 2000}
                ]
            }

        async def fake_generate(request, form_data, user, *, bypass_filter=False):
            captured_form_data.update(form_data)
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        async def channel_budget(_channel_id):
            return None

        async def no_ensure(*_args):
            return False

        monkeypatch.setattr(orchestrator, "get_channel_config", channel_config)
        monkeypatch.setattr(orchestrator, "_channel_budget", channel_budget)
        monkeypatch.setattr(orchestrator, "ensure_collab_tool", no_ensure)

        import open_webui.utils.chat as chat_utils
        monkeypatch.setattr(chat_utils, "generate_chat_completion", fake_generate)

        config = CollabConfig(enabled=True, agents=["agent-a"])
        channel = SimpleNamespace(id="c1")
        result = await orchestrator._quick_completion(
            None, None, channel, config, "agent-a",
            "system prompt", "user prompt", "test_task"
        )
        assert result == "ok"
        assert captured_form_data.get("max_tokens") == 2000

    asyncio.run(scenario())


def test_short_tasks_get_a_small_default_max_tokens(monkeypatch):
    """Una mà alçada no reserva milers de tokens per retornar un JSON curt."""

    async def scenario():
        captured_form_data = {}

        async def no_channel_config(_channel_id):
            return None

        async def fake_generate(request, form_data, user, *, bypass_filter=False):
            captured_form_data.update(form_data)
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"intervene": false, "priority": 1}'
                        }
                    }
                ],
                "usage": {},
            }

        async def channel_budget(_channel_id):
            return None

        monkeypatch.setattr(orchestrator, "get_channel_config", no_channel_config)
        monkeypatch.setattr(orchestrator, "_channel_budget", channel_budget)

        import open_webui.utils.chat as chat_utils
        monkeypatch.setattr(chat_utils, "generate_chat_completion", fake_generate)

        config = CollabConfig(enabled=True, agents=["agent-a"])
        result = await orchestrator._quick_completion(
            None,
            None,
            SimpleNamespace(id="c1"),
            config,
            "agent-a",
            "system prompt",
            "user prompt",
            "handraise",
        )

        assert result is not None
        assert captured_form_data["max_tokens"] == 1024

    asyncio.run(scenario())


def test_t11_profile_priority_affects_handraise_order(monkeypatch):
    """T11 — profile_priority afecta l'ordre de handraise (desempat)."""
    from open_webui.collab.profiles import resolve_agent as _resolve

    async def scenario():
        # profile_priority: a1=1, a2=5 (priority d'handraise igual a 5 per tots dos)
        async def channel_config(_channel_id):
            return {
                "agent_overrides": [
                    {"model_id": "a1", "priority": 1},
                    {"model_id": "a2", "priority": 5},
                ]
            }

        async def fake_completion(_request, _user, _channel, _config, agent_id, *_args, **_kwargs):
            return '{"intervene": true, "priority": 5, "reason": "ok"}'

        async def empty_text(*_args):
            return ""

        async def free_phase(*_args):
            return "free"

        async def no_down(*_args):
            return {}

        async def capture_transition(_channel_id, _seq, agent_id, state):
            pass

        monkeypatch.setattr(orchestrator, "get_channel_config", channel_config)
        monkeypatch.setattr(orchestrator, "_quick_completion", fake_completion)
        monkeypatch.setattr(orchestrator, "build_transcript", empty_text)
        monkeypatch.setattr(orchestrator, "_board_text", empty_text)
        monkeypatch.setattr(orchestrator, "_current_phase", free_phase)
        monkeypatch.setattr(orchestrator, "get_down_agents", no_down)
        monkeypatch.setattr(orchestrator, "_transition_receipt", capture_transition)

        config = CollabConfig(enabled=True, agents=["a1", "a2"])
        volunteers, responded, asked = await orchestrator.handraise(
            None, SimpleNamespace(id="c1"), config, None,
            {"a1": {"name": "A1"}, "a2": {"name": "A2"}},
            None, 1,
        )
        # a2 té profile_priority=5 > a1 té profile_priority=1 → a2 va primer
        assert volunteers == ["a2", "a1"]

    asyncio.run(scenario())


def test_t12_resolved_agent_error_falls_back_safely(monkeypatch):
    """T12 — _resolved_agent amb error fa fallback segur."""

    async def scenario():
        async def raise_error(_channel_id):
            raise RuntimeError("DB down")

        monkeypatch.setattr(orchestrator, "get_channel_config", raise_error)
        result = await orchestrator._resolved_agent("c1", "agent-a")
        assert result.get("priority") == 3
        assert result.get("model_id") == "agent-a"

    asyncio.run(scenario())


def test_b5_budget_notice_deduplicated(monkeypatch):
    """B5 — Avis de pressupost es deduplica (no es repeteix per al mateix reason)."""
    orchestrator._budget_notices.clear()

    async def scenario():
        notices = []

        async def notice(_request, _channel, _user, message):
            notices.append(message)

        async def exhausted(*_args):
            return SimpleNamespace(allowed=False, degraded=False, reason="límit", action="downgrade")

        monkeypatch.setattr(orchestrator, "check_budget", exhausted)
        monkeypatch.setattr(orchestrator, "post_notice", notice)
        monkeypatch.setattr(orchestrator, "request_stop", lambda *_a: True)
        channel = SimpleNamespace(id="dedup-channel")

        # Primera crida: avís
        await orchestrator._budget_model_or_none(
            None, channel, None, "agent-a", "turn", {"fallback_model_id": None}
        )
        # Segona crida amb el mateix reason: no avís
        await orchestrator._budget_model_or_none(
            None, channel, None, "agent-b", "turn", {"fallback_model_id": None}
        )
        assert len(notices) == 1
        orchestrator._budget_notices.clear()

    asyncio.run(scenario())


def test_b6_budget_notice_cleared_when_allowed(monkeypatch):
    """B6 — Budget netejat quan allowed torna a ser True."""
    orchestrator._budget_notices.clear()

    async def scenario():
        notices = []

        async def notice(_request, _channel, _user, message):
            notices.append(message)

        state = {"call": 0}

        async def check_budget(*_args):
            state["call"] += 1
            if state["call"] == 1:
                return SimpleNamespace(allowed=False, degraded=False, reason="límit", action="downgrade")
            return SimpleNamespace(allowed=True, degraded=False, reason="", action="")

        monkeypatch.setattr(orchestrator, "check_budget", check_budget)
        monkeypatch.setattr(orchestrator, "post_notice", notice)
        monkeypatch.setattr(orchestrator, "request_stop", lambda *_a: True)
        channel = SimpleNamespace(id="clear-channel")

        # Primera crida: exhaurit, avís
        await orchestrator._budget_model_or_none(
            None, channel, None, "agent-a", "turn", {"fallback_model_id": None}
        )
        assert "dedup-channel" not in orchestrator._budget_notices  # de T6
        assert "clear-channel" in orchestrator._budget_notices

        # Segona crida: allowed, neteja l'entrada
        result = await orchestrator._budget_model_or_none(
            None, channel, None, "agent-a", "turn", {}
        )
        assert result == "agent-a"
        assert "clear-channel" not in orchestrator._budget_notices

    asyncio.run(scenario())


def test_b7_no_budget_allows_all(monkeypatch):
    """B7 — Sense budget configurat, tot permès."""

    async def scenario():
        async def channel_budget(_channel_id):
            return None

        async def check_budget(*_args):
            return SimpleNamespace(allowed=True, degraded=False, reason="", action="")

        monkeypatch.setattr(orchestrator, "_channel_budget", channel_budget)
        monkeypatch.setattr(orchestrator, "check_budget", check_budget)
        channel = SimpleNamespace(id="no-budget-channel")

        result = await orchestrator._budget_model_or_none(
            None, channel, None, "agent-a", "turn", {}
        )
        assert result == "agent-a"

    asyncio.run(scenario())


def test_b8_channel_budget_error_returns_none(monkeypatch):
    """B8 — _channel_budget amb error retorna None."""

    async def scenario():
        async def raise_error(_channel_id):
            raise RuntimeError("DB down")

        monkeypatch.setattr(orchestrator, "get_channel_config", raise_error)
        result = await orchestrator._channel_budget("c1")
        assert result is None

    asyncio.run(scenario())
