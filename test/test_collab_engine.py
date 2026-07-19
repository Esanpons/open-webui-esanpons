import asyncio
import importlib.metadata

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab.engine import (
    CollabEvent,
    CollabReceipt,
    CollabSession,
    CollabState,
    acquire_lease,
    append_event,
    create_receipts,
    get_state_value,
    list_events,
    list_receipts,
    record_user_message,
    receipt_summary,
    release_lease,
    renew_lease,
    set_state_value,
    supersede_handraises,
    transition_receipt,
    update_receipt,
)
import open_webui.collab.engine as collab_engine


async def _sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'collab.db'}")
    async with engine.begin() as connection:
        for table in (
            CollabSession.__table__,
            CollabEvent.__table__,
            CollabReceipt.__table__,
            CollabState.__table__,
        ):
            await connection.run_sync(table.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_events_are_monotonic_and_receipts_are_idempotent(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                first = await append_event("c1", "user_message", message_id="m1", db=db)
                await db.commit()
            async with sessions() as db:
                second = await append_event("c1", "handraise", agent_id="a1", db=db)
                await db.commit()
            assert (first.seq, second.seq) == (1, 2)

            async with sessions() as db:
                await create_receipts("c1", first.seq, ["a1", "a2", "a1"], message_id="m1", db=db)
                await create_receipts("c1", first.seq, ["a1", "a2"], message_id="m1", db=db)
                await db.commit()
            async with sessions() as db:
                assert await receipt_summary("c1", first.seq, db=db) == {
                    "received": 2,
                    "incorporated": 0,
                    "evaluating": 0,
                    "will_intervene": 0,
                    "pass": 0,
                    "total": 2,
                }
                assert await update_receipt("c1", first.seq, "a1", "evaluating", db=db)
                assert await update_receipt("c1", first.seq, "a1", "will_intervene", db=db)
                assert await update_receipt("c1", first.seq, "a2", "pass", db=db)
                await db.commit()
            async with sessions() as db:
                assert await receipt_summary("c1", first.seq, db=db) == {
                    "received": 0,
                    "incorporated": 0,
                    "evaluating": 0,
                    "will_intervene": 1,
                    "pass": 1,
                    "total": 2,
                }
                receipts = await list_receipts("c1", first.seq, db=db)
                assert [(item.agent_id, item.state) for item in receipts] == [
                    ("a1", "will_intervene"),
                    ("a2", "pass"),
                ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_lease_is_exclusive_and_releasable(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                assert await acquire_lease("c1", "worker-1", db=db)
                await db.commit()
            async with sessions() as db:
                assert not await acquire_lease("c1", "worker-2", db=db)
                await db.commit()
            async with sessions() as db:
                assert await renew_lease("c1", "worker-1", db=db)
                assert not await renew_lease("c1", "worker-2", db=db)
                await db.commit()
            async with sessions() as db:
                assert await release_lease("c1", "worker-1", db=db)
                await db.commit()
            async with sessions() as db:
                assert await acquire_lease("c1", "worker-2", db=db)
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_supersede_handraises_is_idempotent(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                one = await append_event("c1", "handraise", agent_id="a1", db=db)
                two = await append_event("c1", "user_message", message_id="m2", db=db)
                await db.commit()
            async with sessions() as db:
                assert await supersede_handraises("c1", two.seq, db=db) == 1
                assert await supersede_handraises("c1", two.seq, db=db) == 0
                await db.commit()
            async with sessions() as db:
                events = await list_events("c1", db=db)
                assert [event.seq for event in events] == [one.seq, two.seq]
                assert events[0].status == "superseded"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_record_user_message_is_atomic_and_supersedes_old_handraises(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                old = await append_event("c1", "handraise", agent_id="a1", db=db)
                await db.commit()
            async with sessions() as db:
                event = await record_user_message(
                    "c1", ["a1", "a2"], message_id="m1", db=db
                )
                await db.commit()
            async with sessions() as db:
                events = await list_events("c1", db=db)
                assert [(item.seq, item.status) for item in events] == [
                    (old.seq, "superseded"),
                    (event.seq, "active"),
                ]
                assert (await receipt_summary("c1", event.seq, db=db))["received"] == 2
            async with sessions() as db:
                state_event, summary = await transition_receipt(
                    "c1", event.seq, "a1", "evaluating", db=db
                )
                await db.commit()
                assert state_event.type == "agent_state"
                assert state_event.payload["receipt_event_seq"] == event.seq
                assert summary["evaluating"] == 1
            async with sessions() as db:
                events = await list_events("c1", db=db)
                assert [item.type for item in events] == [
                    "handraise",
                    "user_message",
                    "agent_state",
                ]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_receipt_summaries_are_aggregated_and_messages_are_independent(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                first = await record_user_message(
                    "c1", ["a1", "a2", "a3"], message_id="m1", db=db
                )
                second = await record_user_message(
                    "c1", ["a1", "a2", "a3"], message_id="m2", db=db
                )
                await transition_receipt("c1", first.seq, "a1", "evaluating", db=db)
                await transition_receipt("c1", first.seq, "a2", "will_intervene", db=db)
                await transition_receipt("c1", first.seq, "a3", "pass", db=db)
                await db.commit()
            async with sessions() as db:
                first_summary = await receipt_summary("c1", first.seq, db=db)
                second_summary = await receipt_summary("c1", second.seq, db=db)
                assert first_summary == {
                    "received": 0,
                    "incorporated": 0,
                    "evaluating": 1,
                    "will_intervene": 1,
                    "pass": 1,
                    "total": 3,
                }
                assert second_summary["received"] == second_summary["total"] == 3
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_expired_lease_can_be_acquired_by_another_worker(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        now = [1000]
        monkeypatch.setattr(collab_engine.time, "time", lambda: now[0])
        try:
            async with sessions() as db:
                assert await acquire_lease("c1", "worker-1", ttl=1, db=db)
                await db.commit()
            now[0] += 2
            async with sessions() as db:
                assert await acquire_lease("c1", "worker-2", db=db)
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_events_are_gapless_and_resync_honours_since_limit(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)

        async def insert_one():
            async with sessions() as db:
                event = await append_event("c1", "test", db=db)
                await db.commit()
                return event.seq

        try:
            seqs = await asyncio.gather(*[insert_one() for _ in range(50)])
            assert sorted(seqs) == list(range(1, 51))
            async with sessions() as db:
                assert [event.seq for event in await list_events("c1", since=45, db=db)] == [
                    46,
                    47,
                    48,
                    49,
                    50,
                ]
                assert [
                    event.seq for event in await list_events("c1", since=0, limit=10, db=db)
                ] == list(range(1, 11))
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_repeated_receipt_transition_keeps_state_and_orders_events(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                message = await record_user_message("c1", ["a1"], message_id="m1", db=db)
                first, _ = await transition_receipt(
                    "c1", message.seq, "a1", "evaluating", db=db
                )
                second, _ = await transition_receipt(
                    "c1", message.seq, "a1", "evaluating", db=db
                )
                await db.commit()
            async with sessions() as db:
                receipts = await list_receipts("c1", message.seq, db=db)
                assert receipts[0].state == "evaluating"
                assert first.seq < second.seq
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_user_message_is_deduplicated_across_concurrent_workers(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)

        async def record(agents):
            async with sessions() as db:
                event = await record_user_message(
                    "c1", agents, message_id="same-message", db=db
                )
                await db.commit()
                return event.seq

        try:
            seqs = await asyncio.gather(record(["a1"]), record(["a2"]))
            assert seqs == [1, 1]
            async with sessions() as db:
                events = await list_events("c1", db=db)
                assert [(event.seq, event.message_id) for event in events] == [
                    (1, "same-message")
                ]
                receipts = await list_receipts("c1", 1, db=db)
                assert [receipt.agent_id for receipt in receipts] == ["a1", "a2"]
                next_event = await append_event("c1", "test", db=db)
                await db.commit()
                assert next_event.seq == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tests addicionals (specs A2, B1, C1, C2) — Z.ai.glm-5.2
# Aquests tests només depenen d'engine.py (no toquen orchestrator.py).
# ---------------------------------------------------------------------------


def test_agent_full_pass_cycle_received_evaluating_pass(tmp_path):
    """Spec A2 — Agent que rep el missatge i decideix no intervenir (pass).

    Cicle: received → evaluating → pass.
    Verifica que el summary final és correcte i que els events agent_state
    tenen el state adient.
    """
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                msg = await record_user_message(
                    "c1", ["a1"], message_id="m1", db=db
                )
                await db.commit()

            # After record_user_message: receipt in "received"
            async with sessions() as db:
                summary = await receipt_summary("c1", msg.seq, db=db)
                assert summary == {
                    "received": 1,
                    "incorporated": 0,
                    "evaluating": 0,
                    "will_intervene": 0,
                    "pass": 0,
                    "total": 1,
                }

            # Transition to "evaluating"
            async with sessions() as db:
                event_eval, summary_eval = await transition_receipt(
                    "c1", msg.seq, "a1", "evaluating", db=db
                )
                await db.commit()
                assert event_eval.type == "agent_state"
                assert event_eval.payload["state"] == "evaluating"
                assert summary_eval["evaluating"] == 1
                assert summary_eval["received"] == 0

            # Transition to "pass"
            async with sessions() as db:
                event_pass, summary_pass = await transition_receipt(
                    "c1", msg.seq, "a1", "pass", db=db
                )
                await db.commit()
                assert event_pass.type == "agent_state"
                assert event_pass.payload["state"] == "pass"
                assert summary_pass == {
                    "received": 0,
                    "incorporated": 0,
                    "evaluating": 0,
                    "will_intervene": 0,
                    "pass": 1,
                    "total": 1,
                }

            # Verify events are monotonic and have correct states
            async with sessions() as db:
                events = await list_events("c1", db=db)
                types = [e.type for e in events]
                assert types == ["user_message", "agent_state", "agent_state"]
                agent_states = [
                    e.payload["state"] for e in events if e.type == "agent_state"
                ]
                assert agent_states == ["evaluating", "pass"]
                # Monotonic seq
                seqs = [e.seq for e in events]
                assert seqs == sorted(seqs)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_one_receipt_per_agent_for_user_message(tmp_path):
    """Spec B1 — Un missatge humà genera exactament N receipts (un per agent).

    Canal amb 3 agents; verifica que receipt_summary.total == 3 i
    received == 3, i que list_receipts conté exactament els 3 agents.
    """
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                msg = await record_user_message(
                    "c1", ["a1", "a2", "a3"], message_id="m1", db=db
                )
                await db.commit()

            async with sessions() as db:
                summary = await receipt_summary("c1", msg.seq, db=db)
                assert summary["total"] == 3
                assert summary["received"] == 3

                receipts = await list_receipts("c1", msg.seq, db=db)
                assert len(receipts) == 3
                agent_ids = {r.agent_id for r in receipts}
                assert agent_ids == {"a1", "a2", "a3"}
                # All start in "received"
                assert all(r.state == "received" for r in receipts)
                # All reference the same event_seq
                assert all(r.event_seq == msg.seq for r in receipts)
                # All carry the message_id
                assert all(r.message_id == "m1" for r in receipts)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_second_user_message_supersedes_active_handraises(tmp_path):
    """Spec C1 — Missatge humà durant ronda activa: handraises invalidats.

    Un handraise actiu queda "superseded" quan entra un segon missatge humà,
    mentre que els events agent_state es mantenen "active".
    Es creen receipts nous per al seq del nou missatge.
    """
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            # First user message
            async with sessions() as db:
                msg1 = await record_user_message(
                    "c1", ["a1", "a2"], message_id="m1", db=db
                )
                await db.commit()

            # Agent a1 raises hand (active handraise event)
            async with sessions() as db:
                handraise = await append_event(
                    "c1", "handraise", agent_id="a1", db=db
                )
                await db.commit()

            # Agent a1 transitions to will_intervene (creates agent_state event)
            async with sessions() as db:
                agent_ev, _ = await transition_receipt(
                    "c1", msg1.seq, "a1", "will_intervene", db=db
                )
                await db.commit()

            # Second user message arrives (preemption)
            async with sessions() as db:
                msg2 = await record_user_message(
                    "c1", ["a1", "a2"], message_id="m2", db=db
                )
                await db.commit()

            # Verify
            async with sessions() as db:
                events = await list_events("c1", db=db)
                # Find events by type
                handraise_events = [e for e in events if e.type == "handraise"]
                agent_state_events = [e for e in events if e.type == "agent_state"]
                user_message_events = [e for e in events if e.type == "user_message"]

                # The handraise must be superseded
                assert len(handraise_events) == 1
                assert handraise_events[0].status == "superseded"

                # Agent_state events remain active (not handraises)
                assert len(agent_state_events) == 1
                assert agent_state_events[0].status == "active"

                # Both user_messages are active
                assert all(e.status == "active" for e in user_message_events)
                assert len(user_message_events) == 2

                # New receipts created for msg2.seq
                summary2 = await receipt_summary("c1", msg2.seq, db=db)
                assert summary2["total"] == 2
                assert summary2["received"] == 2

                # Old receipts for msg1.seq are untouched
                summary1 = await receipt_summary("c1", msg1.seq, db=db)
                assert summary1["will_intervene"] == 1
                assert summary1["received"] == 1  # a2 still in received

            # Verify msg2.seq > handraise.seq (seq grew past the handraise)
            assert msg2.seq > handraise.seq
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_handraise_receipt_starts_at_received(tmp_path):
    """Spec C2 — Un handraise genera receipts amb estat inicial 'received'.

    Verifica que create_receipts per un event handraise crea receipts
    amb state="received", com qualsevol altre tipus d'event.
    """
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                event = await append_event(
                    "c1", "handraise", agent_id="a1", db=db
                )
                await create_receipts(
                    "c1", event.seq, ["a1", "a2"], db=db
                )
                await db.commit()

            async with sessions() as db:
                receipts = await list_receipts("c1", event.seq, db=db)
                assert len(receipts) == 2
                assert all(r.state == "received" for r in receipts)
                agent_ids = {r.agent_id for r in receipts}
                assert agent_ids == {"a1", "a2"}

                summary = await receipt_summary("c1", event.seq, db=db)
                assert summary["received"] == 2
                assert summary["total"] == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# W4-3: CollabState key-value store — Z.ai.glm-5.2
# ---------------------------------------------------------------------------


def test_state_value_get_returns_default_when_missing(tmp_path):
    """W4-3 — get_state_value retorna default quan la clau no existeix,
    i set_state_value fa upsert correctament."""
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                # Key doesn't exist → returns default
                result = await get_state_value(
                    "c1", "summary", default="none", db=db
                )
                assert result == "none"

                # Set value
                await set_state_value("c1", "summary", "hello world", db=db)
                await db.commit()

            async with sessions() as db:
                # Get returns the set value
                result = await get_state_value("c1", "summary", db=db)
                assert result == "hello world"

            async with sessions() as db:
                # Upsert: overwrite existing key
                await set_state_value("c1", "summary", "updated", db=db)
                await db.commit()

            async with sessions() as db:
                result = await get_state_value("c1", "summary", db=db)
                assert result == "updated"

                # Different channel has its own state
                result2 = await get_state_value(
                    "c2", "summary", default="other", db=db
                )
                assert result2 == "other"

            async with sessions() as db:
                # JSON values (dict) work too
                await set_state_value(
                    "c1", "down_agents", {"a1": "timeout"}, db=db
                )
                await db.commit()

            async with sessions() as db:
                result = await get_state_value("c1", "down_agents", db=db)
                assert result == {"a1": "timeout"}

                # Different key on same channel is independent
                summary = await get_state_value("c1", "summary", db=db)
                assert summary == "updated"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_state_value_concurrent_set_is_safe(tmp_path):
    """W4-3 — Diversos workers fan set_state_value concurrentment;
    l'última escriptura guanya, no hi ha inserts duplicats."""
    async def scenario():
        engine, sessions = await _sessions(tmp_path)

        async def writer(value):
            async with sessions() as db:
                await set_state_value("c1", "phase", value, db=db)
                await db.commit()

        try:
            await asyncio.gather(*[writer(f"phase-{i}") for i in range(10)])

            async with sessions() as db:
                result = await get_state_value("c1", "phase", db=db)
                assert result is not None
                assert result.startswith("phase-")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
