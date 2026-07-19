# Disseny W11/W12 — Model de dades de perfils i personalització d'agents

> Autor: Z.ai.glm-5.2 · Data: 17/07/2026
> Depèn de: res (és disseny pur; la implementació és de Claude Fable a Fase 3).
> Bloqueja: W13/W14 frontend (necessita `agent_overrides` per als colors/avatars de W14 i els modes de W13).

---

## 1. Visió general

Dos conceptes separats però connectats:

```
collab_profile (plantilla)                collab_channel_config (còpia efectiva)
┌──────────────────────────┐              ┌──────────────────────────┐
│ id, user_id, name        │              │ channel_id (PK)           │
│ config (CollabConfig)    │── apply ──▶  │ config (CollabConfig)     │
│ agent_overrides (JSON)   │              │ agent_overrides (JSON)    │
│ budget (JSON, nullable)  │              │ budget (JSON, nullable)   │
│ is_template              │              │ source_profile_id         │
│ created_at, updated_at   │              │ source_profile_version    │
└──────────────────────────┘              │ version (optimista)       │
                                          │ updated_at                │
                                          └──────────────────────────┘
                                                    │
                                                    │ save_as_profile
                                                    ▼
                                          (crea un collab_profile nou
                                           amb l'estat efectiu actual)
```

- **El perfil** és la plantilla reutilitzable: es crea, edita, duplica, exporta i comparteix.
- **La configuració efectiva** viu en `collab_channel_config`, independent del perfil. Un cop aplicat el perfil, els canvis al canal no toquen el perfil original.
- **Cap dels dos** viu en `channel.meta` — tanca S1 estructuralment.

---

## 2. Taula `collab_profile`

```sql
CREATE TABLE collab_profile (
  id              TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL,
  name            TEXT NOT NULL,
  description     TEXT,
  config          JSON NOT NULL DEFAULT '{}',      -- CollabConfig serialitzat
  agent_overrides JSON NOT NULL DEFAULT '[]',      -- llista d'AgentOverride
  budget          JSON,                             -- null = il·limitat
  is_template     BOOLEAN NOT NULL DEFAULT 0,
  updated_at      BIGINT NOT NULL,
  created_at      BIGINT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES user(id)
);

CREATE INDEX idx_collab_profile_user ON collab_profile(user_id);
CREATE INDEX idx_collab_profile_template ON collab_profile(is_template) WHERE is_template = 1;
```

---

## 3. Taula `collab_channel_config`

```sql
CREATE TABLE collab_channel_config (
  channel_id            TEXT PRIMARY KEY,
  source_profile_id     TEXT,                       -- nullable: si es va començar en blanc
  source_profile_version BIGINT,                    -- version del perfil quan es va copiar
  config                JSON NOT NULL DEFAULT '{}', -- CollabConfig serialitzat
  agent_overrides       JSON NOT NULL DEFAULT '[]', -- llista d'AgentOverride
  budget                JSON,                        -- null = il·limitat
  version               INTEGER NOT NULL DEFAULT 1, -- actualització optimista
  updated_at            BIGINT NOT NULL,
  FOREIGN KEY (channel_id) REFERENCES channel(id)
);
```

**Actualització optimista:**

```sql
UPDATE collab_channel_config
SET config = :config,
    agent_overrides = :overrides,
    budget = :budget,
    version = version + 1,
    updated_at = :now
WHERE channel_id = :cid AND version = :expected_version;
-- 0 files afectades = algú ha escrit primer (conflicte).
```

**Per què no cal `BEGIN IMMEDIATE`:**
- És una sola fila per canal (PK `channel_id`).
- L'UPDATE amb `WHERE ... AND version = :expected` és atòmic en SQLite.
- Les actualitzacions són poc freqüents (modificació manual, no per missatge).

---

## 4. Estructura `AgentOverride`

Cada element d'`agent_overrides` és un JSON amb aquesta forma:

```typescript
interface AgentOverride {
  model_id: string;           // obligatori — ha d'existir a config.agents
  role?: string;              // p.ex. "Arquitecte", "Programador", "Crític"
  system_prompt?: string;     // prompt personalitzat que substitueix el default
  effort?: string;            // "low" | "medium" | "high" — només si el connector ho suporta
  token_limit?: number;       // límit de output tokens per a aquest agent
  tools?: string[];           // llista de tool_ids permesos; absents = tots
  priority?: number;          // 1-5, pes en l'ordre d'intervenció
  color?: string;             // hex, p.ex. "#3B82F6" — alimenta W14
  avatar?: string;            // URL o emoji — alimenta W14
  fallback_model_id?: string; // model de degradació (W15 action_on_exhaustion="downgrade")
}
```

### Validació

- `model_id` **ha d'existir** a `config.agents`. Si no hi és, l'override s'ignora amb warning.
- `effort` només s'envia al proveïdor si el connector del model declara `supports_effort = true` (o equivalent). Si no, el camp es marca com a desactivat a la UI de W12.
- `tools` pot restringir eines; si s'omet, l'agent té accés a totes les eines de la taula rodona.
- `color` i `avatar` són purament visuals (W14); no afecten el comportament.

### Merge amb `CollabConfig`

Quan es construeix el context per un agent:

```python
def resolve_agent(agent_id: str, config: CollabConfig, overrides: list[dict]) -> ResolvedAgent:
    """Fusiona la configuració base amb l'override de l'agent (si n'hi ha)."""
    base = {
        "model_id": agent_id,
        "role": None,
        "system_prompt": None,
        "effort": None,
        "token_limit": None,
        "tools": None,  # None = tots
        "priority": 3,  # default
        "color": None,
        "avatar": None,
        "fallback_model_id": None,
    }
    override = next((o for o in overrides if o.get("model_id") == agent_id), None)
    if override:
        base.update({k: v for k, v in override.items() if v is not None})
    return ResolvedAgent(**base)
```

**Regla:** amb override s'usa el valor de l'override; sense override, comportament actual.

---

## 5. Endpoints d'API

### Perfils

| Mètode | Ruta | Descripció |
|---|---|---|
| `GET` | `/collab/profiles` | Llista els perfils de l'usuari + templates (`is_template=true`). |
| `POST` | `/collab/profiles` | Crea un perfil nou (des de zero o desa l'estat efectiu d'un canal). |
| `GET` | `/collab/profiles/{id}` | Detall d'un perfil. |
| `PUT` | `/collab/profiles/{id}` | Actualitza un perfil. |
| `DELETE` | `/collab/profiles/{id}` | Elimina un perfil (no els templates del sistema). |
| `POST` | `/collab/profiles/{id}/duplicate` | Duplica un perfil amb nom nou. |
| `GET` | `/collab/profiles/{id}/export` | Exporta com a JSON autocontingut (download). |
| `POST` | `/collab/profiles/import` | Importa un JSON (valida estructura abans de crear). |

### Configuració efectiva del canal

| Mètode | Ruta | Descripció |
|---|---|---|
| `POST` | `/collab/channels/{channel_id}/profile/apply` | Copia el perfil a `collab_channel_config`. El perfil original no es toca. |
| `POST` | `/collab/channels/{channel_id}/profile/save` | Desa l'estat efectiu actual com a perfil nou. |
| `GET` | `/collab/channels/{channel_id}/config` | Llegeix la configuració efectiva (config + overrides + budget). |
| `PUT` | `/collab/channels/{channel_id}/config` | Actualitza la configuració efectiva (amb `version` optimista). |

### Flux d'aplicació (`POST .../apply`)

```python
async def apply_profile(channel_id: str, profile_id: str, user) -> bool:
    profile = await get_profile(profile_id, user)
    if not profile:
        raise HTTPException(404, "Perfil no trobat")

    now = int(time.time())
    async with get_async_db_context() as db:
        # Upsert: si ja existeix config pel canal, se substitueix; si no, s'insereix.
        await db.execute(text("""
            INSERT INTO collab_channel_config (
                channel_id, source_profile_id, source_profile_version,
                config, agent_overrides, budget, version, updated_at
            ) VALUES (
                :cid, :pid, :pver,
                :config, :overrides, :budget, 1, :now
            )
            ON CONFLICT(channel_id) DO UPDATE SET
                source_profile_id = :pid,
                source_profile_version = :pver,
                config = :config,
                agent_overrides = :overrides,
                budget = :budget,
                version = collab_channel_config.version + 1,
                updated_at = :now
        """), {
            "cid": channel_id, "pid": profile.id, "pver": profile.updated_at,
            "config": json.dumps(profile.config),
            "overrides": json.dumps(profile.agent_overrides),
            "budget": json.dumps(profile.budget) if profile.budget else None,
            "now": now,
        })
        await db.commit()
    return True
```

### Flux de desar com a perfil nou (`POST .../save`)

```python
async def save_as_profile(channel_id: str, name: str, description: str, user) -> str:
    """Captura l'estat efectiu del canal com a perfil nou."""
    config_row = await get_channel_config(channel_id)
    profile = CollabProfile(
        id=str(uuid4()),
        user_id=user.id,
        name=name,
        description=description,
        config=config_row.config,
        agent_overrides=config_row.agent_overrides,
        budget=config_row.budget,
        is_template=False,
        updated_at=int(time.time()),
        created_at=int(time.time()),
    )
    await save_profile(profile)
    return profile.id
```

---

## 6. Migració des de l'estat actual

Actualment tot viu a `channel.meta['collab']` com un `CollabConfig`. La migració:

1. **No esborrar** `channel.meta['collab']` — segueix sent el `CollabConfig` base (agents, mode, project_dir, guardrails).
2. **Migrar gradualment:** a la primera vegada que un canal amb `collab` activat es llegeix, si no existeix fila a `collab_channel_config`, es crea automàticament amb la `config` actual i `agent_overrides = []` (sense overrides = comportament actual).
3. **Els overrides i el budget** es llegeixen de `collab_channel_config`; el `CollabConfig` base es llegeix de `channel.meta['collab']` (compatibilitat cap enrere) **o** de `collab_channel_config.config` (preferit si existeix).

Això garanteix que cap canal existent es trenqui.

---

## 7. Com encaixa amb l'orquestrador (`orchestrator.py`)

### Canvis mínims a `agent_turn()` i `_handraise_one()`

Actualment:

```python
config = get_collab_config(channel)  # llegeix de channel.meta['collab']
```

Canvi:

```python
config = get_collab_config(channel)           # base: agents, mode, project_dir, guardrails
channel_cfg = await get_channel_config(channel.id)  # nova taula
overrides = channel_cfg.agent_overrides if channel_cfg else []
budget = channel_cfg.budget if channel_cfg else None
```

Després, per cada agent:

```python
resolved = resolve_agent(agent_id, config, overrides)

# Si té system_prompt personalitzat, es fusiona amb la filosofia d'equip
if resolved.system_prompt:
    system = f"Ets {resolved.role or name}, {resolved.system_prompt}\n\n" + _PHILOSOPHY + ...
else:
    system = f"Ets {name}, membre d'un EQUIP..." + _PHILOSOPHY + ...  # actual

# Si té token_limit, es posa al form_data
if resolved.token_limit:
    form_data["max_tokens"] = resolved.token_limit

# Si té effort i el connector ho suporta
if resolved.effort and model_supports_effort(agent_id, models):
    form_data["reasoning_effort"] = resolved.effort

# Si té tools restringits
if resolved.tools is not None:
    form_data["tool_ids"] = [t for t in form_data.get("tool_ids", []) if t in resolved.tools]
```

### Prioritat d'intervenció

Actualment, el `priority` del hand-raise ve del JSON que retorna l'agent (`{"intervene": true, "priority": 1-5}`).

Amb `agent_overrides`, el `priority` de l'override es pot fer servir com a **pes base**:

```python
# A handraise(), quan s'ordena els voluntaris:
resolved_priority = override_priority * 2  # pes de l'override
volunteers.sort(key=lambda v: (-(v["priority"] + resolved_base.get(v["agent"], 0)), order.get(v["agent"], 99)))
```

O més simple: el `priority` de l'override augmenta/reduceix el priority declarat per l'agent. Decisió d'implementació — recomano començar sense tocar la lògica de hand-raise i només usar `priority` com a desempat.

---

## 8. Criteris d'acceptació (W11 + W12)

### W11 — Perfils

1. **CRUD complet:** crear, llegir, actualitzar i eliminar perfils.
2. **Aplicar no muta l'original:** `POST .../apply` copia el perfil a `collab_channel_config`; el perfil original queda intacte.
3. **Export/import JSON autocontingut:** el JSON conté tot el necessari per recrear el perfil (config + overrides + budget + metadata).
4. **Perfils predefinits:** `is_template=true` visibles per a tots els usuaris.
5. **Actualització optimista:** dos writes simultanis amb la mateixa `version` → un falla amb error 409 Conflict, cap es perd silenciosament.
6. **Validació d'agents:** `agent_overrides[*].model_id` ha d'existir a `config.agents`.

### W12 — Personalització

1. **Merge correcte:** amb override s'usa el valor de l'override; sense override, comportament actual.
2. **Effort condicional:** `effort` només s'envia si el connector del model ho suporta; altrament es marca com a desactivat a la UI.
3. **Prompt personalitzat:** si l'agent té `system_prompt`, es fusiona amb la filosofia d'equip (mai la substitueix completament).
4. **Límit de tokens:** `token_limit` es respecta a la crida de l'API.
5. **Color/avatar:** es passen al frontend per a W14.
6. **Fallback model:** `fallback_model_id` disponible per a W15 degradació.

---

## 9. Notes d'implementació per Claude Fable

1. **Pydantic models:** definir `AgentOverride` com a BaseModel per validació automàtica a l'entrada d'API.
2. **Migració Alembic:** una sola migració crea `collab_profile` + `collab_channel_config`. Incloure `downgrade()` que eliminii les dues taules.
3. **No tocar `CollabConfig`** ni `run_round` per la part de perfils — els overrides s'injecten als hooks existents (`agent_turn`, `_handraise_one`).
4. **Compatibilitat:** canals existents sense `collab_channel_config` segueixen funcionant (lazy migration al primer accés).
5. **Rutes d'API:** afegir a `collab/router.py`, seguint el patró existent de `_check_can_manage`.
6. **Test clau:** crear perfil → aplicar → modificar canal → verificar perfil original intacte → exportar → importar → verificar idèntic.
