"""Tests de perfils reutilitzables i personalització d'agents (W11/W12).

Cobreix:
- CRUD de perfils (crear, llegir, actualitzar, esborrar, duplicar).
- Aplicar un perfil a un canal no muta l'original.
- Export/import JSON autocontingut amb validació.
- Versionatge optimista de channel_config (409 Conflict).
- resolve_agent: merge de base + override.
- Lazy migration: ensure_channel_config crea la fila si no existeix.
- display_name propagat a l'orquestrador.
- Plantilles propagen canvis als canals vinculats.
- Edició local desvincula el canal de la plantilla.
- Errors del provider es mostren amb detall tècnic.
- save_as_profile captura TOTA la configuració (inclós canal sense channel_config).
"""

import asyncio
import contextlib
import importlib.metadata

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_real_version = importlib.metadata.version
importlib.metadata.version = (
    lambda name: "0.0.0" if name == "open-webui" else _real_version(name)
)

import open_webui.collab.profiles as profiles_mod
from open_webui.models.channels import Channel
from open_webui.collab.profiles import (
    ChannelConfigForm,
    CollabChannelConfig,
    CollabProfile,
    ProfileForm,
    apply_profile,
    create_profile,
    delete_profile,
    duplicate_profile,
    ensure_channel_config,
    export_profile_json,
    get_channel_config,
    get_profile,
    list_profiles,
    resolve_agent,
    save_as_profile,
    sync_channel_config_from_meta,
    update_channel_config,
    update_profile,
    validate_imported_profile,
)


# ---------------------------------------------------------------------------
# Fixtures: in-memory SQLite amb les dues taules
# ---------------------------------------------------------------------------


async def _setup_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'profiles.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(CollabProfile.__table__.create)
        await conn.run_sync(CollabChannelConfig.__table__.create)
        await conn.run_sync(Channel.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    # Parchejem get_async_db_context perquè les funcions de profiles.py
    # facin servir la nostra sessió de prova en lloc de la BD global.
    @contextlib.asynccontextmanager
    async def _test_ctx():
        async with sessions() as s:
            yield s

    original = profiles_mod.get_async_db_context
    profiles_mod.get_async_db_context = _test_ctx
    return engine, original


def _teardown(engine, original):
    profiles_mod.get_async_db_context = original


async def _insert_channel(channel_id: str, meta: dict | None = None):
    """Insereix un canal de prova amb meta opcional."""
    now = int(__import__("time").time() * 1_000_000)
    async with profiles_mod.get_async_db_context() as db:
        ch = Channel(
            id=channel_id,
            user_id="user1",
            name=f"Channel {channel_id}",
            data={"type": "collab"},
            meta=meta or {},
            created_at=now,
            updated_at=now,
        )
        db.add(ch)
        await db.commit()


# ---------------------------------------------------------------------------
# Tests CRUD perfils
# ---------------------------------------------------------------------------


def test_profile_crud_full_cycle(tmp_path):
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            form = ProfileForm(
                name="Equip de proves",
                description="Config de test",
                config={"enabled": True, "agents": ["a1", "a2"]},
                agent_overrides=[
                    {"model_id": "a1", "role": "Arquitecte", "priority": 5},
                ],
                budget={"daily_tokens": 100000},
            )
            profile = await create_profile("user1", form)
            assert profile["name"] == "Equip de proves"
            assert profile["config"]["agents"] == ["a1", "a2"]
            assert profile["is_template"] is True

            fetched = await get_profile(profile["id"], "user1")
            assert fetched["name"] == "Equip de proves"

            updated_form = ProfileForm(
                name="Equip millorat",
                description="Actualitzat",
                config={"enabled": True, "agents": ["a1", "a2", "a3"]},
                agent_overrides=[],
                budget=None,
            )
            updated = await update_profile(profile["id"], "user1", updated_form)
            assert updated["name"] == "Equip millorat"
            assert updated["budget"] is None

            profiles = await list_profiles("user1")
            assert len(profiles) == 1
            assert profiles[0]["name"] == "Equip millorat"

            assert await delete_profile(profile["id"], "user1")
            assert not await delete_profile("missing", "user1")
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


def test_other_user_cannot_access_profile(tmp_path):
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            form = ProfileForm(name="Privat", config={}, agent_overrides=[])
            profile = await create_profile("user1", form)

            # user2 no pot veure el perfil de user1 (només si és template)
            assert await get_profile(profile["id"], "user2") is None
            # user2 no pot actualitzar-lo
            assert await update_profile(profile["id"], "user2", form) is None
            # user2 no pot esborrar-lo
            assert not await delete_profile(profile["id"], "user2")
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tests apply_profile (no muta l'original)
# ---------------------------------------------------------------------------


def test_apply_profile_copies_and_does_not_mutate_original(tmp_path):
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            form = ProfileForm(
                name="Plantilla",
                config={"enabled": True, "agents": ["a1"]},
                agent_overrides=[
                    {"model_id": "a1", "role": "Líder", "priority": 5}
                ],
                budget={"daily_tokens": 50000},
            )
            profile = await create_profile("user1", form)

            # Apply al canal
            ok, cfg = await apply_profile("channel-1", profile["id"], "user1")
            assert ok
            assert cfg["source_profile_id"] == profile["id"]
            assert cfg["config"]["agents"] == ["a1"]
            assert cfg["agent_overrides"][0]["role"] == "Líder"

            # Modifiquem la config del canal
            update_form = ChannelConfigForm(
                agent_overrides=[{"model_id": "a1", "role": "Programador"}],
                expected_version=1,
            )
            ok2, cfg2 = await update_channel_config("channel-1", update_form)
            assert ok2
            assert cfg2["agent_overrides"][0]["role"] == "Programador"

            # El perfil original NO ha canviat
            original_profile = await get_profile(profile["id"], "user1")
            assert original_profile["agent_overrides"][0]["role"] == "Líder"
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tests versionatge optimista de channel_config
# ---------------------------------------------------------------------------


def test_channel_config_optimistic_versioning(tmp_path):
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            # Crida inicial sense expected_version → sempre ok
            ok, cfg = await update_channel_config(
                "ch1",
                ChannelConfigForm(
                    config={"agents": ["a1"]}, agent_overrides=[], budget=None
                ),
            )
            assert ok
            assert cfg["version"] == 1

            # Crida amb expected_version correcte → ok
            ok2, cfg2 = await update_channel_config(
                "ch1",
                ChannelConfigForm(
                    config={"agents": ["a1", "a2"]},
                    expected_version=1,
                ),
            )
            assert ok2
            assert cfg2["version"] == 2

            # Crida amb expected_version incorrecte → conflict
            ok3, cfg3 = await update_channel_config(
                "ch1",
                ChannelConfigForm(
                    config={"agents": ["x"]},
                    expected_version=1,  # era 2
                ),
            )
            assert not ok3
            assert cfg3["version"] == 2  # no ha canviat
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tests resolve_agent
# ---------------------------------------------------------------------------


def test_resolve_agent_merges_base_and_override():
    overrides = [
        {"model_id": "a1", "role": "Arquitecte", "priority": 5},
        {"model_id": "a2", "role": "Programador", "color": "#FF0000"},
    ]
    resolved_a1 = resolve_agent("a1", overrides)
    assert resolved_a1["role"] == "Arquitecte"
    assert resolved_a1["priority"] == 5
    assert resolved_a1["system_prompt"] is None
    assert resolved_a1["model_id"] == "a1"

    resolved_a2 = resolve_agent("a2", overrides)
    assert resolved_a2["role"] == "Programador"
    assert resolved_a2["color"] == "#FF0000"
    assert resolved_a2["priority"] == 3  # default

    # Agent sense override → tots defaults
    resolved_a3 = resolve_agent("a3", overrides)
    assert resolved_a3["role"] is None
    assert resolved_a3["priority"] == 3
    assert resolved_a3["tools"] is None


def test_resolve_agent_empty_overrides():
    resolved = resolve_agent("any", [])
    assert resolved["model_id"] == "any"
    assert resolved["priority"] == 3
    assert resolved["role"] is None


def test_resolve_agent_display_name():
    """display_name del override s'inclou al resolved."""
    overrides = [
        {"model_id": "a1", "display_name": "Pinya"},
    ]
    resolved = resolve_agent("a1", overrides)
    assert resolved["display_name"] == "Pinya"


# ---------------------------------------------------------------------------
# Tests export/import
# ---------------------------------------------------------------------------


def test_export_import_roundtrip(tmp_path):
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            form = ProfileForm(
                name="Exportable",
                description="Per exportar",
                config={"enabled": True, "agents": ["a1", "a2"], "mode": "handraise"},
                agent_overrides=[
                    {"model_id": "a1", "role": "Cap", "priority": 5},
                ],
                budget={"daily_tokens": 100000},
            )
            profile = await create_profile("user1", form)
            exported = export_profile_json(profile)

            assert exported["format"] == "collab-profile-v2"
            assert exported["name"] == "Exportable"
            assert exported["config"]["agents"] == ["a1", "a2"]

            # Validar import
            ok, error, form_imported = validate_imported_profile(exported)
            assert ok, error
            assert form_imported.name == "Exportable"

            # Importar com a nou perfil
            imported = await create_profile("user2", form_imported)
            assert imported["config"]["agents"] == ["a1", "a2"]
            assert imported["agent_overrides"][0]["role"] == "Cap"
            assert imported["user_id"] == "user2"
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


def test_import_rejects_bad_format():
    bad_data = {"format": "unknown", "name": "Test"}
    ok, error, form = validate_imported_profile(bad_data)
    assert not ok
    assert "collab-profile-v1" in error

    ok2, error2, _ = validate_imported_profile({"format": "collab-profile-v1"})
    assert not ok2
    assert "name" in error2


# ---------------------------------------------------------------------------
# Tests lazy migration (ensure_channel_config)
# ---------------------------------------------------------------------------


def test_ensure_channel_config_creates_if_missing(tmp_path):
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            assert await get_channel_config("ch-new") is None
            cfg = await ensure_channel_config("ch-new", {"agents": ["a1"], "enabled": True})
            assert cfg["channel_id"] == "ch-new"
            assert cfg["config"]["agents"] == ["a1"]
            assert cfg["agent_overrides"] == []
            assert cfg["version"] == 1

            # Segona crida no el recrea
            cfg2 = await ensure_channel_config("ch-new", {"agents": ["x"]})
            assert cfg2["config"]["agents"] == ["a1"]  # no sobreescrit
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


def test_sync_channel_config_from_meta_preserves_customization(tmp_path):
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            await _insert_channel("ch-sync")
            await update_channel_config(
                "ch-sync",
                ChannelConfigForm(
                    config={"enabled": False, "agents": []},
                    agent_overrides=[{"model_id": "a1", "display_name": "Agent A"}],
                    budget={"daily_tokens": 1234},
                ),
            )
            synced = await sync_channel_config_from_meta(
                "ch-sync", {"enabled": True, "agents": ["a1"]}
            )
            assert synced["config"] == {"enabled": True, "agents": ["a1"]}
            assert synced["agent_overrides"][0]["display_name"] == "Agent A"
            assert synced["budget"] == {"daily_tokens": 1234}
            assert synced["source_profile_id"] is None
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tests duplicate_profile
# ---------------------------------------------------------------------------


def test_duplicate_profile_creates_independent_copy(tmp_path):
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            form = ProfileForm(
                name="Original",
                config={"agents": ["a1"]},
                agent_overrides=[{"model_id": "a1", "priority": 4}],
            )
            original = await create_profile("user1", form)
            clone = await duplicate_profile(original["id"], "user1", "Còpia")
            assert clone["name"] == "Còpia"
            assert clone["id"] != original["id"]
            assert clone["config"] == original["config"]
            assert clone["is_template"] is True
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tests save_as_profile — captura TOTA la configuració
# ---------------------------------------------------------------------------


def test_save_channel_as_profile_captures_effective_state(tmp_path):
    """Desa el canal quan té tant channel_config com channel.meta.collab."""
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            await _insert_channel("ch-save", meta={
                "collab": {
                    "enabled": True,
                    "agents": ["a1", "a2"],
                    "project_dir": "C:/projecte",
                    "mode": "roundrobin",
                    "conversation_mode": "continuous",
                    "guardrails": {"turn_timeout": 0, "context_messages": 42},
                }
            })

            # Creem channel_config amb overrides
            await update_channel_config(
                "ch-save",
                ChannelConfigForm(
                    config={  # això quedarà sobreescrit per channel.meta.collab
                        "enabled": True,
                        "agents": ["a1"],
                        "mode": "handraise",
                    },
                    agent_overrides=[{
                        "model_id": "a1",
                        "display_name": "Laia",
                        "role": "Líder",
                        "system_prompt": "Revisa-ho tot",
                        "avatar": "🧭",
                        "color": "#2563eb",
                        "tools": ["collab_workspace"],
                        "priority": 5,
                    }],
                    budget={"daily_tokens": 20000},
                ),
            )
            # Simula una edició posterior des del panell general. Aquest panell
            # escriu l'estat visible a channel.meta.collab, que és el que ha de
            # prevaldre quan es desa la fotografia completa del canal.
            async with profiles_mod.get_async_db_context() as db:
                result = await db.execute(
                    profiles_mod.select(profiles_mod.Channel).where(
                        profiles_mod.Channel.id == "ch-save"
                    )
                )
                channel = result.scalar_one()
                channel.meta = {
                    **(channel.meta or {}),
                    "collab": {
                        "enabled": True,
                        "agents": ["a1", "a2"],
                        "project_dir": "C:/projecte",
                        "mode": "roundrobin",
                        "conversation_mode": "continuous",
                        "guardrails": {
                            "turn_timeout": 0,
                            "context_messages": 42,
                        },
                    },
                }
                await db.commit()
            profile = await save_as_profile("ch-save", "Desat del canal", "Test", "user1")
            assert profile is not None
            assert profile["name"] == "Desat del canal"

            # channel.meta.collab guanya sobre channel_config.config
            assert profile["config"]["agents"] == ["a1", "a2"]
            assert profile["config"]["project_dir"] == "C:/projecte"
            assert profile["config"]["mode"] == "roundrobin"
            assert profile["config"]["conversation_mode"] == "continuous"
            assert profile["config"]["guardrails"]["turn_timeout"] == 0

            # Els overrides i el pressupost venen de channel_config
            assert profile["agent_overrides"][0]["display_name"] == "Laia"
            assert profile["agent_overrides"][0]["avatar"] == "🧭"
            assert profile["agent_overrides"][0]["tools"] == ["collab_workspace"]
            assert profile["budget"] == {"daily_tokens": 20000}
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


def test_save_as_profile_works_without_channel_config(tmp_path):
    """save_as_profile ha de funcionar quan el canal NO té collab_channel_config.

    Aquest és el cas normal: l'usuari configura el canal des del panell general
    (agents, carpeta, modes, guardrails), que escriu a channel.meta.collab,
    però mai no obre la secció de personalització d'agents (que crea la fila
    collab_channel_config). El bug anterior feia que save_as_profile retornés
    None i el perfil no es desava."""
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            await _insert_channel("ch-nocc", meta={
                "collab": {
                    "enabled": True,
                    "agents": ["a1", "a2", "a3"],
                    "project_dir": "D:/el-meu-projecte",
                    "mode": "handraise",
                    "conversation_mode": "rounds",
                    "guardrails": {"turn_timeout": 600, "context_messages": 20},
                }
            })
            # NO creem collab_channel_config — aquesta era la condició del bug
            assert await get_channel_config("ch-nocc") is None

            profile = await save_as_profile("ch-nocc", "Plantilla sencera", "", "user1")
            # Abans del fix això retornava None
            assert profile is not None
            assert profile["name"] == "Plantilla sencera"
            assert profile["config"]["agents"] == ["a1", "a2", "a3"]
            assert profile["config"]["project_dir"] == "D:/el-meu-projecte"
            assert profile["config"]["mode"] == "handraise"
            assert profile["config"]["conversation_mode"] == "rounds"
            assert profile["config"]["guardrails"]["turn_timeout"] == 600
            # Sense channel_config → overrides buits, budget None
            assert profile["agent_overrides"] == []
            assert profile["budget"] is None
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


def test_save_as_profile_captures_overrides_from_channel_config(tmp_path):
    """Si hi ha channel_config amb overrides però no channel.meta.collab,
    els overrides i la config de channel_config es desen correctament."""
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            await _insert_channel("ch-cc-only")
            await update_channel_config(
                "ch-cc-only",
                ChannelConfigForm(
                    config={
                        "enabled": True,
                        "agents": ["x1"],
                        "mode": "roundrobin",
                    },
                    agent_overrides=[
                        {"model_id": "x1", "display_name": "X1", "avatar": "🤖"},
                    ],
                ),
            )
            profile = await save_as_profile("ch-cc-only", "CC-only", "", "user1")
            assert profile is not None
            assert profile["config"]["agents"] == ["x1"]
            assert profile["config"]["mode"] == "roundrobin"
            assert profile["agent_overrides"][0]["display_name"] == "X1"
            assert profile["agent_overrides"][0]["avatar"] == "🤖"
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tests propagació de plantilles
# ---------------------------------------------------------------------------


def test_update_profile_propagates_to_linked_channels(tmp_path):
    """Quan s'edita una plantilla, els canals vinculats s'actualitzen."""
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            form = ProfileForm(
                name="Plantilla original",
                config={"enabled": True, "agents": ["a1"], "mode": "handraise"},
                agent_overrides=[{"model_id": "a1", "role": "Líder"}],
            )
            profile = await create_profile("user1", form)

            # Apply al canal
            ok, cfg = await apply_profile("ch1", profile["id"], "user1")
            assert ok
            assert cfg["config"]["agents"] == ["a1"]

            # Ara editem la plantilla
            updated_form = ProfileForm(
                name="Plantilla actualitzada",
                config={"enabled": True, "agents": ["a1", "a2", "a3"], "mode": "roundrobin"},
                agent_overrides=[{"model_id": "a1", "role": "Cap"}],
            )
            await update_profile(profile["id"], "user1", updated_form)

            # El canal vinculat ha rebut els canvis
            linked_cfg = await get_channel_config("ch1")
            assert linked_cfg["config"]["agents"] == ["a1", "a2", "a3"]
            assert linked_cfg["config"]["mode"] == "roundrobin"
            assert linked_cfg["agent_overrides"][0]["role"] == "Cap"
            assert linked_cfg["source_profile_id"] == profile["id"]
            assert linked_cfg["version"] > 1  # la versió s'ha incrementat
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


def test_local_edit_unlinks_channel_from_profile(tmp_path):
    """Una edició local del canal el desvincula de la plantilla."""
    async def scenario():
        engine, original = await _setup_db(tmp_path)
        try:
            form = ProfileForm(
                name="Plantilla",
                config={"enabled": True, "agents": ["a1"]},
                agent_overrides=[{"model_id": "a1", "role": "Líder"}],
            )
            profile = await create_profile("user1", form)
            ok, _ = await apply_profile("ch1", profile["id"], "user1")
            assert ok

            # Editem localment el canal
            ok2, cfg2 = await update_channel_config(
                "ch1",
                ChannelConfigForm(
                    config={"agents": ["a1", "a2"]},
                    expected_version=1,
                ),
            )
            assert ok2
            # El canal ha perdut el source_profile_id
            assert cfg2["source_profile_id"] is None

            # Si actualitzem la plantilla ara, el canal NO canvia
            updated_form = ProfileForm(
                name="Plantilla nova",
                config={"enabled": True, "agents": ["a3"], "mode": "roundrobin"},
                agent_overrides=[],
            )
            await update_profile(profile["id"], "user1", updated_form)

            linked_cfg = await get_channel_config("ch1")
            assert linked_cfg["config"]["agents"] == ["a1", "a2"]  # no ha canviat
        finally:
            _teardown(engine, original)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tests display_name a l'orquestrador
# ---------------------------------------------------------------------------


def test_agent_display_name_prefers_override():
    """_agent_display_name: display_name del override > nom del model > agent_id."""
    from open_webui.collab import orchestrator

    # Override amb display_name → guanya
    assert orchestrator._agent_display_name(
        {"display_name": "Pinya"}, {"name": "Claude Fable"}, "claude-cli"
    ) == "Pinya"

    # Sense display_name → nom del model
    assert orchestrator._agent_display_name(
        {}, {"name": "Claude Fable"}, "claude-cli"
    ) == "Claude Fable"

    # Sense res → agent_id
    assert orchestrator._agent_display_name({}, None, "claude-cli") == "claude-cli"
