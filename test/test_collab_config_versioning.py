"""Tests per al versionatge optimista de collab config (W4-6).

Verifica que save_collab_config detecta conflictes amb expected_version i que
la columna meta_version s'incrementa correctament.
"""

import asyncio
import importlib.metadata
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_real_version = importlib.metadata.version
importlib.metadata.version = (
    lambda name: "0.0.0" if name == "open-webui" else _real_version(name)
)

from open_webui.collab.config import CollabConfig, get_collab_config, save_collab_config
import open_webui.collab.config as collab_config
from open_webui.models.channels import Channel, ChannelModel


async def _sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'config.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Channel.__table__.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _insert_channel(sessions, channel_id="c1", meta=None):
    async with sessions() as db:
        db.add(
            Channel(
                id=channel_id,
                user_id="u1",
                type="group",
                name="test",
                data=None,
                meta=meta or {},
                meta_version=0,
                created_at=0,
                updated_at=0,
            )
        )
        await db.commit()


def _use_test_sessions(monkeypatch, sessions):
    @asynccontextmanager
    async def test_db_context():
        async with sessions() as db:
            yield db

    monkeypatch.setattr(collab_config, "get_async_db_context", test_db_context)


async def _get_channel_model(sessions, channel_id="c1") -> ChannelModel:
    from sqlalchemy import select

    async with sessions() as db:
        result = await db.execute(select(Channel).filter(Channel.id == channel_id))
        row = result.scalars().first()
        return ChannelModel(
            id=row.id,
            user_id=row.user_id,
            type=row.type,
            name=row.name,
            description=None,
            is_private=None,
            data=row.data,
            meta=row.meta,
            meta_version=row.meta_version or 0,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


def test_save_without_expected_version_always_succeeds(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        _use_test_sessions(monkeypatch, sessions)
        try:
            await _insert_channel(sessions)
            config = CollabConfig(enabled=True, agents=["a1", "a2"])

            ok, version = await save_collab_config("c1", config)
            assert ok is True
            assert version == 1

            model = await _get_channel_model(sessions)
            assert model.meta_version == 1
            stored = get_collab_config(model)
            assert stored.enabled is True
            assert stored.agents == ["a1", "a2"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_save_with_correct_expected_version_succeeds(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        _use_test_sessions(monkeypatch, sessions)
        try:
            await _insert_channel(sessions)
            config = CollabConfig(enabled=True, agents=["a1"])

            ok, version = await save_collab_config("c1", config)
            assert ok is True
            assert version == 1

            config2 = CollabConfig(enabled=True, agents=["a1", "a2"])
            ok, version = await save_collab_config("c1", config2, expected_version=1)
            assert ok is True
            assert version == 2

            model = await _get_channel_model(sessions)
            assert model.meta_version == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_save_with_wrong_expected_version_returns_conflict(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        _use_test_sessions(monkeypatch, sessions)
        try:
            await _insert_channel(sessions)

            # First write bumps version to 1
            config1 = CollabConfig(enabled=True, agents=["a1"])
            ok, version = await save_collab_config("c1", config1)
            assert version == 1

            # Simulate a stale client that read version 0 before the first write:
            # it tries to save with expected_version=0, but current is 1 → conflict
            config2 = CollabConfig(enabled=True, agents=["a2"])
            ok, current_version = await save_collab_config("c1", config2, expected_version=0)
            assert ok is False
            assert current_version == 1  # tells the client what the real version is

            # The config was NOT overwritten
            model = await _get_channel_model(sessions)
            assert model.meta_version == 1
            stored = get_collab_config(model)
            assert stored.agents == ["a1"]  # original, not overwritten
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_saves_one_wins_one_loses(tmp_path, monkeypatch):
    async def scenario():
        engine, sessions = await _sessions(tmp_path)
        _use_test_sessions(monkeypatch, sessions)
        try:
            await _insert_channel(sessions)

            # Two concurrent saves, both expecting version 0
            config_a = CollabConfig(enabled=True, agents=["a"])
            config_b = CollabConfig(enabled=True, agents=["b"])

            results = await asyncio.gather(
                save_collab_config("c1", config_a, expected_version=0),
                save_collab_config("c1", config_b, expected_version=0),
            )

            wins = sum(1 for ok, _ in results if ok)
            assert wins == 1, f"Expected exactly 1 success, got {wins}"

            # The winner has version 1; the loser knows the current version
            for ok, ver in results:
                if ok:
                    assert ver == 1
                else:
                    assert ver == 1  # current version reported back
        finally:
            await engine.dispose()

    asyncio.run(scenario())
