# Disseny W4 — Persistència d'entrada col·laborativa + escriptura atòmica

> Autor: Z.ai.glm-5.2 · Data: 17/07/2026
> Dependències: motor W1/W9/W10 (`engine.py`, taules persistents), `files.py`, `config.py`, `tasks.py`
> Coordinació: Codex Sol (orquestrador, endpoints); Claude Fable (frontend)
> W4 resol: S1 (race read-modify-write sobre `channel.meta`), S2 (escriptura no atòmica),
>           S7 (sense límit de mida a `write_text_file`), S8 (estat barrejat en `channel.meta`)

---

## 1. Objectiu

W4 garanteix que l'estat col·laboratiu persisteix de forma fiable quan coincideixen operacions
concurrents. Avui queden dos punts pendents després del motor W1/W9/W10:

1. **Race read-modify-write sobre `channel.meta`** (S1) — `save_collab_config` i els endpoints
   de tasques (`create_task`, `update_task`, `delete_task`) llegeixen el JSON sencer de
   `channel.meta`, el modifiquen en Python i l'escriuen de tornada sense `FOR UPDATE` ni
   versionatge optimista. Dos agents escrivint alhora poden perdre canvis silenciosament.
2. **Escriptura de fitxers no atòmica** (S2) — `write_text_file` a `files.py` fa
   `target.write_text(content)` directament: un crash a mig escriure deixa un fitxer parcial.
3. **Sense límit de mida a `write_text_file`** (S7) — un agent pot escriure un fitxer
   il·limitadament gran i saturar el disc/projecte.
4. **Estat de tasques i resum barrejat a `channel.meta`** (S8) — tasques, resum, fase i
   agents caiguts viuen com a JSON dins `channel.meta`, participant del mateix read-modify-write.

### 1.1 El que ja està resolt

El motor W1/W9/W10 (`engine.py`) ja ha mogut fora de `channel.meta`:
- ✅ Sessions i lease (`collab_session`)
- ✅ Event log (`collab_event`)
- ✅ Receipts per agent (`collab_receipt`)
- ✅ Telemetria i pressupostos (`collab_usage`, `collab_budget_tracker`)

El que queda dins `channel.meta` és:
- `collab` (`CollabConfig`: agents, mode, project_dir, guardrails, conversation_mode)
- `collab_tasks` (llista de tasques)
- `collab_summary` (resum incremental)
- `collab_phase` (planning/execution)
- `collab_down_agents` (agents caiguts)

---

## 2. S2 + S7 — Escriptura atòmica i límit de mida de fitxers

### 2.1 Escriptura atòmica: `tmp + os.replace()`

**Problema actual** (`files.py:write_text_file`):
```python
target.write_text(content, encoding="utf-8")
```
Si el procés mor aquí, el fitxer queda a mig escriure (corrupte).

**Solució**: escriure a un fitxer temporal al mateix directori, llavors `os.replace()`
(atómic en POSIX i Windows):

```python
import tempfile
import os

def write_text_file(project_dir: str, relative: str, content: str) -> tuple[bool, str]:
    target = resolve_safe(project_dir, relative)
    if target is None:
        return False, f"Ruta fora del projecte: {relative}"
    if target.is_dir():
        return False, f"És una carpeta: {relative}"

    # S7: límit de mida
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        return False, f"Contingut massa gran ({len(data)} bytes; màxim {MAX_FILE_BYTES})."

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Escriure a temporal al mateix directori (perquè os.replace sigui atòmic).
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=".collab_write_",
            suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, target)
        except BaseException:
            # Netejar el temporal si algo falla
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
            raise
        return True, f"Escrit {relative} ({len(data)} bytes)."
    except OSError as e:
        return False, f"Error escrivint {relative}: {e}"
```

**Propietats:**
- `os.replace()` és atòmic tant a Windows com a POSIX (no hi ha mai un fitxer parcial visible).
- El fitxer temporal és al mateix directori perquè `os.replace()` no funciona entre dispositius.
- Si el procés mor, el temporal queda orfe (prefix `.collab_write_`) — es pot netejar periòdicament.
- `MAX_FILE_BYTES` (512 KB) també s'aplica a `write_text_file`, no només a `read_text_file`.

### 2.2 Neteja de temporals orfes

```python
def cleanup_temp_files(project_dir: str) -> int:
    """Elimina fitxers .collab_write_*.tmp orfes d'escriptures interrompudes."""
    root = Path(project_dir)
    count = 0
    if not root.is_dir():
        return 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".git")]
        for f in filenames:
            if f.startswith(".collab_write_") and f.endswith(".tmp"):
                try:
                    (Path(dirpath) / f).unlink()
                    count += 1
                except OSError:
                    pass
    return count
```

Es crida periòdicament o en arrencar, per eliminar temporals d'un procés que ha mort.

---

## 3. S1 — Versionatge optimista per a `save_collab_config`

### 3.1 Problema actual

```python
# config.py — save_collab_config
channel.meta = {**(channel.meta or {}), "collab": config.model_dump()}
await db.commit()
```

Si dos operacions llegeixen `channel.meta` quasi alhora, modifiquen la seva part i
escriuen, la segona sobrecriu la primera (last-write-wins silenciós).

**Escenari crític**: l'usuari canvia els agents al panell mentre un agent fa
`/collab guardrail max_agent_turns=5` per tool. Un dels dos canvis es perd.

### 3.2 Solució: column `meta_version` + compare-and-set

Afegir un camp `meta_version` (BigInteger) a la taula `channel`:

```python
# A models/channels.py (o el model que sigui):
class Channel(Base):
    # ... camps existents ...
    meta_version = Column(BigInteger, nullable=False, default=0)
```

**Operació compare-and-set:**

```python
# config.py
async def save_collab_config(
    channel_id: str,
    config: CollabConfig,
    *,
    expected_version: int | None = None,
) -> str | None:
    """Desa la config amb versionatge optimista.

    Si expected_version no és None, comprova que la versió actual coincideixi.
    Retorna la nova versió, o None si hi ha conflicte (expected_version no coincideix).
    """
    async with get_async_db_context() as db:
        result = await db.execute(
            select(Channel).filter(Channel.id == channel_id)
        )
        channel = result.scalars().first()
        if not channel:
            return None

        if expected_version is not None and channel.meta_version != expected_version:
            return None  # conflicte detectat

        new_meta = {**(channel.meta or {}), "collab": config.model_dump()}
        new_version = (channel.meta_version or 0) + 1

        await db.execute(
            update(Channel)
            .where(
                Channel.id == channel_id,
                Channel.meta_version == (channel.meta_version or 0),
            )
            .values(meta=new_meta, meta_version=new_version)
        )
        await db.commit()
        return new_version
```

**Flux del client (frontend/agent):**
1. Llegir config (inclou `meta_version`).
2. Modificar localment.
3. Cridar `save_collab_config(expected_version=version_llegida)`.
4. Si retorna `None`: algú ha canviat la config mentrestant → rellegir, fusionar, reintentar.

### 3.3 Migració

```sql
ALTER TABLE channel ADD COLUMN meta_version BIGINT NOT NULL DEFAULT 0;
```

Tots els canals existents comencen amb `meta_version=0`. No hi ha dada perduda.

### 3.4 Endpoints del router

El router ha de retornar `meta_version` al `GET /config` i acceptar-lo al `POST /config`:

```python
@router.get("/{channel_id}/config")
async def get_config(...):
    # ...
    return {
        **config.model_dump(),
        "meta_version": channel.meta_version or 0,  # nou
        # ... resta de camps existents ...
    }

@router.post("/{channel_id}/config")
async def update_config(..., form_data: CollabConfigForm):
    # ...
    new_version = await save_collab_config(
        channel.id, config, expected_version=form_data.expected_meta_version
    )
    if new_version is None:
        raise HTTPException(
            status_code=409,
            detail="La configuració ha canviat mentrestant. Refresca i reintenta.",
        )
    # ...
```

**Codi HTTP 409 Conflict** és el codi semànticament correcte per a optimistic locking.

---

## 4. S8 — Migrar tasques, resum, fase i agents caiguts fora de `channel.meta`

### 4.1 Taula `collab_state`

Les dades restants dins `channel.meta` són col·laboratives però no són
configuració estructural com `CollabConfig`. Es moguen a una taula pròpia:

```python
# A engine.py o un nou fitxer collab_state.py
class CollabState(Base):
    """Estat col·laboratiu per canal: tasques, resum, fase, agents caiguts.

    Cadascun és una fila independent — no hi ha JSON global ni
    read-modify-write de tot el bloc.
    """
    __tablename__ = "collab_state"
    __table_args__ = (
        UniqueConstraint("channel_id", "key", name="uq_collab_state_channel_key"),
    )

    channel_id = Column(Text, nullable=False)
    key = Column(Text, nullable=False)  # "summary" | "phase" | "down_agents"
    value = Column(JSONField, nullable=True)
    updated_at = Column(BigInteger, nullable=False)
    # PK composta: (channel_id, key) via UniqueConstraint + autofill
    id = Column(Text, primary_key=True)
```

### 4.2 Per què no una fila amb columnes per cada camp?

Perquè cada clau pot ser escrita per un agent diferent sense interferir-hi. Una fila
per clau significa que `set_summary` no toca `set_phase`. Amb una sola fila amb
columnes, un `UPDATE ... SET summary=...` no causaria conflicte (cada column és
independent), però un JSON global sí (S1 de tornada). La fila-per-clau és el
patró més net.

### 4.3 Tasques: taula pròpia `collab_task`

Les tasques avui són una llista JSON dins `channel.meta['collab_tasks']`.
Cada creació/modificació/esborrat és un read-modify-write de tota la llista.

```python
class CollabTask(Base):
    __tablename__ = "collab_task"

    id = Column(Text, primary_key=True)
    channel_id = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="pending")
    assignee = Column(Text, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    created_by = Column(Text, nullable=False, default="")
    created_at = Column(BigInteger, nullable=False)
    updated_at = Column(BigInteger, nullable=False)
```

Amb índexs:
```sql
CREATE INDEX idx_collab_task_channel ON collab_task(channel_id);
CREATE INDEX idx_collab_task_status ON collab_task(channel_id, status);
```

**Avantatge**: `create_task` és un `INSERT` directe (atómic), no un read-modify-write.
`update_task` és un `UPDATE ... WHERE id=...` (atómic). Cap race possible.

### 4.4 Funcions d'accés

```python
# A un nou mòdul collab/state.py o dins engine.py:

async def get_state_value(channel_id: str, key: str, default=None, *, db=None):
    async with _session_scope(db) as (session, owns):
        result = await session.execute(
            select(CollabState.value).where(
                CollabState.channel_id == channel_id,
                CollabState.key == key,
            )
        )
        val = result.scalar_one_or_none()
        return val if val is not None else default

async def set_state_value(channel_id: str, key: str, value, *, db=None):
    now = int(time.time())
    async with _session_scope(db) as (session, owns):
        insert_fn = _insert_for(session, CollabState)
        await session.execute(
            insert_fn
            .values(
                id=str(uuid.uuid4()),
                channel_id=channel_id,
                key=key,
                value=value,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["channel_id", "key"],
                set_={"value": value, "updated_at": now},
            )
        )
        if owns:
            await session.commit()
        else:
            await session.flush()


# Resum (usa get_state_value / set_state_value amb key="summary")
async def get_summary(channel_id: str, *, db=None) -> str | None:
    return await get_state_value(channel_id, "summary", db=db)

async def set_summary(channel_id: str, summary: str, *, db=None) -> None:
    await set_state_value(channel_id, "summary", summary, db=db)


# Fase
async def get_phase(channel_id: str, *, db=None) -> str:
    return await get_state_value(channel_id, "phase", "planning", db=db)

async def set_phase(channel_id: str, phase: str, *, db=None) -> None:
    await set_state_value(channel_id, "phase", phase, db=db)


# Agents caiguts
async def get_down_agents(channel_id: str, *, db=None) -> dict:
    return await get_state_value(channel_id, "down_agents", {}, db=db)

async def set_down_agent(channel_id: str, agent_id: str, reason: str, *, db=None) -> None:
    current = await get_down_agents(channel_id, db=db)
    current[agent_id] = {"reason": reason, "since": int(time.time())}
    await set_state_value(channel_id, "down_agents", current, db=db)

async def clear_down_agent(channel_id: str, agent_id: str, *, db=None) -> bool:
    current = await get_down_agents(channel_id, db=db)
    if agent_id not in current:
        return False
    del current[agent_id]
    await set_state_value(channel_id, "down_agents", current, db=db)
    return True
```

### 4.5 Migració de dades existents

```sql
CREATE TABLE collab_state (...);
CREATE TABLE collab_task (...);

-- Migrar dades existents des de channel.meta:
-- Per cada canal amb collab actiu:
--   INSERT INTO collab_state (channel_id, key, value, updated_at)
--   SELECT id, 'summary', json_extract(meta, '$.collab_summary'), ...
--   FROM channel WHERE json_extract(meta, '$.collab_summary') IS NOT NULL;
--
--   (similar per 'phase' i 'down_agents')
--
--   Per tasques: iterar el JSON de channel.meta['collab_tasks']
--   i fer INSERT a collab_task per cadascuna.
```

La migració és reversible: el downgrade elimina les taules noves sense tocar
`channel.meta` (que conserva les seves claus originals).

### 4.6 Observació: `down_agents` encara és read-modify-write

Tot i moure `down_agents` a `collab_state`, la lògica de `set_down_agent` i
`clear_down_agent` segueix sent read-modify-write sobre el JSON de la fila
`collab_state`. Això és acceptable perquè:

1. El volum de canvis és molt baix (un agent caigut es detecta rarament).
2. El conflicte més greu seria perdre un `clear_down_agent` (l'agent queda com
   caigut quan en realitat ja s'ha recuperat) — però l'orquestrador detecta
   la recuperació automàticament i reactiva l'agent al proper torn.
3. Si es volgués eliminar completament, es podria moure a una taula per agent
   (`collab_agent_health`), però no val la pena pel volum actual.

---

## 5. Canvis a `tasks.py`

El fitxer `tasks.py` avui implementa les operacions sobre `channel.meta['collab_tasks']`.
Es migren a `collab_task`:

```python
# tasks.py — versió nova

async def get_tasks(channel_id: str, *, db=None) -> list[dict]:
    """Tasques del canal, ordenades per data de creació."""
    async with get_async_db_context(db) as session:
        result = await session.execute(
            select(CollabTask)
            .where(CollabTask.channel_id == channel_id)
            .order_by(CollabTask.created_at.asc())
        )
        return [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "assignee": t.assignee,
                "notes": t.notes,
                "created_by": t.created_by,
            }
            for t in result.scalars().all()
        ]

async def create_task(
    channel_id: str, title: str, *, created_by: str = "", assignee: str = "", db=None
) -> dict:
    now = int(time.time())
    task = CollabTask(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        title=title,
        status="pending",
        assignee=assignee,
        notes="",
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    async with get_async_db_context(db) as session:
        session.add(task)
        await session.commit()
    return {"id": task.id, "title": title, "status": "pending",
            "assignee": assignee, "notes": "", "created_by": created_by}

async def update_task(
    channel_id: str, task_id: str, *, title: str = "", status: str = "",
    assignee: str = "", notes: str = "", db=None
) -> tuple[bool, str]:
    async with get_async_db_context(db) as session:
        result = await session.execute(
            select(CollabTask).where(
                CollabTask.id == task_id,
                CollabTask.channel_id == channel_id,
            )
        )
        task = result.scalars().first()
        if not task:
            return False, "Tasca no trobada"
        if title:
            task.title = title
        if status:
            task.status = status
        if assignee:
            task.assignee = assignee
        if notes:
            task.notes = notes
        task.updated_at = int(time.time())
        await session.commit()
    return True, "ok"

async def delete_task(channel_id: str, task_id: str, *, db=None) -> bool:
    async with get_async_db_context(db) as session:
        result = await session.execute(
            select(CollabTask).where(
                CollabTask.id == task_id,
                CollabTask.channel_id == channel_id,
            )
        )
        task = result.scalars().first()
        if not task:
            return False
        await session.delete(task)
        await session.commit()
    return True
```

---

## 6. Integració amb el codi existent

### 6.1 `orchestrator.py`

Els canvis són mínims:
- `_board_text` crida `get_summary`, `get_tasks`, `get_down_agents` — només canvia
  la implementació subacent (ja no llegeix `channel.meta`).
- `_update_summary` crida `set_summary` — mateix patró.
- `get_phase`/`set_phase` — mateixa API, implementació nova.
- `_mark_agent_down`/`_mark_agent_up` — usen `set_down_agent`/`clear_down_agent`.

### 6.2 `router.py`

- `GET /config` retorna `meta_version`.
- `POST /config` accepta `expected_meta_version` i retorna 409 si hi ha conflicte.
- Els endpoints de tasques (`POST /tasks`, `POST /tasks/:id`, `DELETE /tasks/:id`)
  continen tenint la mateixa API; només canvia la implementació subacent.

### 6.3 `commands.py`

- `/collab guardrails clau=valor` — usa `save_collab_config` amb versionatge optimista.
- `/collab status` — llegeix de les noves fonts de dades.

### 6.4 `file_tools.py`

L'eina `write_project_file` que els agents criden via tools ha d'usar la nova
`write_text_file` atòmica. Si `file_tools.py` crida `files.write_text_file`
directament, el canvi és transparent.

### 6.5 Frontend

- `CollabPanel.svelte`:
  - `getCollabConfig` retorna `meta_version` — guardar-lo.
  - `updateCollabConfig` envia `expected_meta_version`.
  - Si rep 409: mostrar toast "Config modificada per un altre procés, refrescant…"
    i rellegir la config.

---

## 7. Migració de la BD

### 7.1 Fitxer de migració Alembic

```python
# migrations/versions/a1b2c3d4e5f6_add_collab_state_and_tasks.py

revision = "a1b2c3d4e5f6"
down_revision = "f6a7b8c9d0e1"  # o el head collab vigent

def upgrade():
    # Column meta_version a channel
    op.add_column("channel", sa.Column("meta_version", sa.BigInteger(), nullable=False, server_default="0"))

    # Taula collab_state
    op.create_table(
        "collab_state",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", open_webui.internal.db.JSONField, nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("channel_id", "key", name="uq_collab_state_channel_key"),
    )
    op.create_index("idx_collab_state_channel", "collab_state", ["channel_id"])

    # Taula collab_task
    op.create_table(
        "collab_task",
        sa.Column("id", sa.Text(), primary_key=True),
        su.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("assignee", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("idx_collab_task_channel", "collab_task", ["channel_id"])
    op.create_index("idx_collab_task_status", "collab_task", ["channel_id", "status"])

    # Migrar dades existents des de channel.meta
    # (SQL raw o Python loop segons el que sigui més net per Alembic)
    _migrate_state_from_meta()

def downgrade():
    op.drop_table("collab_task")
    op.drop_table("collab_state")
    op.drop_column("channel", "meta_version")
```

### 7.2 Funció de migració de dades

```python
def _migrate_state_from_meta():
    """Migra summary, phase, down_agents i tasques des de channel.meta a les noves taules."""
    import json
    import uuid
    import time
    from sqlalchemy import select, text
    from open_webui.internal.db import get_sync_db_context  # o async

    # Per cada canal amb collab actiu:
    # 1. Extreure de meta['collab_summary'] → INSERT INTO collab_state
    # 2. Extreure de meta['collab_phase'] → INSERT INTO collab_state
    # 3. Extreure de meta['collab_down_agents'] → INSERT INTO collab_state
    # 4. Iterar meta['collab_tasks'] → INSERT INTO collab_task per cadascuna
    #
    # No modificar meta en aquesta migració: les claus originals romanen
    # per compatibilitat (es poden netejar en una migració posterior).
    pass  # Implementació concreta depén de la versió de SQLAlchemy/Alembic
```

---

## 8. Ordre d'implementació incremental

| Pas | Descripció | Fitxer | Provable? |
|---|---|---|---|
| **W4-1** | Escriptura atòmica + límit de mida a `write_text_file` | `files.py` | ✅ Unitari: escriure + crash simulat + verificar fitxer no parcial |
| **W4-2** | Neteja de temporals orfes | `files.py` | ✅ Unitari: crear temp + cleanup |
| **W4-3** | Taula `collab_state` + funcions `get/set_state_value` | `engine.py` + migració | ✅ Unitari: get/set/get None/default |
| **W4-4** | Taula `collab_task` + reimplementar `tasks.py` | `engine.py` + `tasks.py` + migració | ✅ Unitari: CRUD complet |
| **W4-5** | Migrar `get_summary`/`set_summary`/`get_phase`/`set_phase`/`get_down_agents`/`set_down_agent` | `engine.py` + `tasks.py` (o `state.py`) | ✅ Tests existents de `tasks.py` han de seguir passant |
| **W4-6** | `meta_version` + versionatge optimista a `save_collab_config` | `config.py` + model `channel` + migració | ✅ Unitari: dos writes concurrents → un 409 |
| **W4-7** | Frontend: enviar `expected_meta_version` + gestionar 409 | `collab/index.ts` + `CollabPanel.svelte` | ✅ Manual: canviar config mentre agent fa guardrails |

Cada pas és independent i desplegable per separat. **W4-1 i W4-2 es poden fer immediatament**
sense migracions. W4-3 a W4-5 requereixen la migració. W4-6 és el més delicat
perquè toca el model `channel`.

---

## 9. Criteris d'acceptació

### S2 — Escriptura atòmica
- [ ] `write_text_file` escriu a un fitxer temporal i fa `os.replace()`.
- [ ] Un crash a mig escriure NO deixa un fitxer parcial visible.
- [ ] `os.replace()` és atòmic tant a Windows com a POSIX.

### S7 — Límit de mida
- [ ] `write_text_file` rebutja continguts > `MAX_FILE_BYTES` (512 KB).
- [ ] El missatge d'error és clar.

### S1 — Versionatge optimista
- [ ] `save_collab_config` amb `expected_version` que no coincideix retorna `None` (o 409 al REST).
- [ ] Dos modifications concurrents: una guanya, l'altra rep 409.
- [ ] El client pot rellegir, fusionar i reintentar.

### S8 — Estat fora de `channel.meta`
- [ ] `get_tasks`/`create_task`/`update_task`/`delete_task` operen sobre `collab_task`.
- [ ] `get_summary`/`set_summary` operen sobre `collab_state`.
- [ ] `get_phase`/`set_phase` operen sobre `collab_state`.
- [ ] `get_down_agents`/`set_down_agent`/`clear_down_agent` operen sobre `collab_state`.
- [ ] `channel.meta` només conté `collab` (`CollabConfig`).
- [ ] Els tests existents (24 proves de `test_collab_engine.py`) continen passant.
- [ ] Migració reversible: `downgrade` elimina les taules noves.

---

## 10. Riscos i mitigacions

| Risc | Mitigació |
|---|---|
| `os.replace()` entre dispositius falla | El temporal és al mateix directori del destí |
| Fitxer temporal orfe si el procés mor | Netjeja periòdica + prefix `.collab_write_` identificable |
| Migració perd dada de tasques/resum | Migració reversible: `channel.meta` conserva les claus originals |
| `meta_version` = 0 per canals existents | Tots comencen amb 0; primera escriptura sempre guanya |
| 409 freqüent si l'usuari i un agent escriuen alhora | El client reintentà automàticament (rellegir + fusionar) |
| `collab_state` read-modify-write per `down_agents` | Acceptat (volum baix, recuperació automàtica) |
| Tests existents trenquen | Reimplementar les mateixes APIs; tests han de passar sense canvis |

---

## 11. Coordinació amb l'equip

- **Codex Sol:** responsable principal d'implementar la migració i els canvis a `config.py`,
  `tasks.py` i el model `channel`. W4-1 i W4-2 (escriptura atòmica) no toquen el seu
  territori i es poden fer independentment.
- **Claude Fable:** frontend — gestionar 409 amb reintent i toast. Mínim canvi.
- **Z.ai.glm-5.2:** aquest disseny + revisió de concurrència de la migració + proves
  de no-regressió (tests existents han de passar) + proves de concurrència nova.

**Ordre d'edició:** `files.py` (W4-1, W4-2) → migració i `engine.py` (W4-3, W4-4) →
`tasks.py` (W4-5) → `config.py` + model (W4-6) → frontend (W4-7).
