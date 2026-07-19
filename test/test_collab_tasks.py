import asyncio
import importlib.metadata

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_real_version = importlib.metadata.version
importlib.metadata.version = (
    lambda name: "0.0.0" if name == "open-webui" else _real_version(name)
)

from open_webui.collab.engine import CollabState, CollabTask
from open_webui.collab.tasks import (
    clear_down_agent,
    create_task,
    delete_task,
    get_down_agents,
    get_phase,
    get_summary,
    get_tasks,
    set_down_agent,
    set_phase,
    set_summary,
    update_task,
)


async def _sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(CollabState.__table__.create)
        await connection.run_sync(CollabTask.__table__.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_task_crud_is_row_scoped_and_atomic(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                first = await create_task(
                    "c1", "Primera", created_by="agent-a", assignee="agent-b", db=db
                )
                second = await create_task("c1", "Segona", db=db)
                await create_task("c2", "Altre canal", db=db)
                await db.commit()

            async with sessions() as db:
                tasks = await get_tasks("c1", db=db)
                assert [task["title"] for task in tasks] == ["Primera", "Segona"]
                ok, _ = await update_task(
                    "c1",
                    first["id"],
                    status="doing",
                    notes="en curs",
                    db=db,
                )
                assert ok
                assert not (await update_task("c2", first["id"], status="done", db=db))[0]
                assert await delete_task("c1", second["id"], db=db)
                assert not await delete_task("c1", "missing", db=db)
                await db.commit()

            async with sessions() as db:
                tasks = await get_tasks("c1", db=db)
                assert tasks == [
                    {
                        "id": first["id"],
                        "title": "Primera",
                        "status": "doing",
                        "assignee": "agent-b",
                        "notes": "en curs",
                        "created_by": "agent-a",
                    }
                ]
                assert len(await get_tasks("c2", db=db)) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_operational_state_uses_collab_state(tmp_path):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        try:
            async with sessions() as db:
                assert await get_summary("c1", db=db) == ""
                assert await get_phase("c1", db=db) == "planning"
                await set_summary("c1", "resum", db=db)
                await set_phase("c1", "execution", db=db)
                await set_down_agent("c1", "a1", "timeout", db=db)
                await db.commit()

            async with sessions() as db:
                assert await get_summary("c1", db=db) == "resum"
                assert await get_phase("c1", db=db) == "execution"
                assert (await get_down_agents("c1", db=db))["a1"]["reason"] == "timeout"
                assert await clear_down_agent("c1", "a1", db=db)
                assert not await clear_down_agent("c1", "missing", db=db)
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
