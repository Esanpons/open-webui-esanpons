# Disseny detallat del motor W1/W9/W10 — Visibilitat, escolta garantida i conversa fluida

> Autor: Z.ai.glm-5.2 · Data: 17/07/2026
> Dependències: Fase 0 (telemetria) ha d'estar integrada; W0 desbloquejat per a proves.
> Codex Sol és el responsable principal d'implementació d'aquest bloc.

---

## 1. Objectiu

Transformar `orchestrator.py` (avui: rondes síncrones, polling, estat en memòria) en un sistema
amb tres capacitats noves:

1. **W1 — Visibilitat d'agents:** màquina d'estats per agent + barra d'agents al canal; l'usuari
   sap qui escolta, pensa, executa, espera o ha caigut, en temps real via socket.
2. **W9 — Escolta garantida:** cada missatge humà genera `collab_receipt` per agent amb estats
   `received → incorporated → evaluating → will_intervene/pass`; resum visible `Rebut per X/N`.
3. **W10 — Conversa fluida:** scheduler basat en esdeveniments amb lease persistent; els missatges
   de l'usuari tenen prioritat i poden interrompre la cua de torns; la ronda deixa de ser barrera.

---

## 2. Arquitectura: tres taules noves (§3.4 del pla)

Les tres taules neixen separades de `channel.meta`. Cap read-modify-write sobre JSON.

### 2.1 `collab_session` — lifecycle + lease per canal

```sql
CREATE TABLE collab_session (
  channel_id     TEXT PRIMARY KEY,
  status         TEXT NOT NULL DEFAULT 'idle',   -- active | idle | stopped
  lease_owner    TEXT,                             -- worker_id que té el lock
  lease_expires_at BIGINT,                         -- epoch segons
  last_event_seq INTEGER NOT NULL DEFAULT 0,       -- per reprendre després de reinici
  updated_at     BIGINT NOT NULL
);
```

**Adquisició del lease (compare-and-set atòmic):**

```sql
UPDATE collab_session
SET lease_owner = :wid, lease_expires_at = :now + 30
WHERE channel_id = :cid
  AND (lease_owner = :wid OR lease_expires_at < :now);
```

Retorna 0 files → un altre worker té el lease. Renovació cada 10s. TTL 30s → si el worker mor,
un altre el recull automàticament.

**Cicle de vida:** `idle` (sense ronda) → `active` (ronda/scheduler en marxa) → `idle` (silenci
o parada). `stopped` = desactivat manualment.

### 2.2 `collab_event` — log append-only ordenat per seq

```sql
CREATE TABLE collab_event (
  id          TEXT PRIMARY KEY,
  channel_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,            -- monòton per canal
  type        TEXT NOT NULL,                -- user_message | agent_message | handraise |
                                            --   turn_start | turn_end | agent_state | cancel
  agent_id    TEXT,
  message_id  TEXT,
  payload     JSON,
  status      TEXT NOT NULL DEFAULT 'active', -- active | superseded | consumed
  created_at  BIGINT NOT NULL,
  UNIQUE(channel_id, seq)
);
```

**Inserció amb seq monòton (BEGIN IMMEDIATE a SQLite):**

```sql
BEGIN IMMEDIATE;
INSERT INTO collab_event VALUES (
  :id, :cid,
  (SELECT COALESCE(MAX(seq), 0) + 1 FROM collab_event WHERE channel_id = :cid),
  :type, :agent_id, :message_id, :payload, 'active', :now
);
COMMIT;
```

El `BEGIN IMMEDIATE` adquireix el write-lock abans del SELECT, garantint que dues insercions
concurrents no llegeixen el mateix MAX(seq).

**Invalidació idempotent (per W10):**

```sql
UPDATE collab_event SET status = 'superseded'
WHERE channel_id = :cid AND seq < :N AND type = 'handraise' AND status = 'active';
```

`superseded` és terminal; executar-ho 10 vegades deixa el mateix estat.

### 2.3 `collab_receipt` — traçabilitat per agent (W9)

```sql
CREATE TABLE collab_receipt (
  id          TEXT PRIMARY KEY,
  event_seq   INTEGER NOT NULL,            -- FK implícita a collab_event.seq
  channel_id  TEXT NOT NULL,
  agent_id    TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'received',
                                            -- received → incorporated → evaluating
                                            --   → will_intervene | pass
  message_id  TEXT,
  updated_at  BIGINT NOT NULL,
  UNIQUE(event_seq, agent_id)
);
```

Cada missatge humà genera una fila per agent. El resum visible (`Rebut per 3/3 · 2 valorant · 1
vol intervenir`) és una agregació simple per `state`.

---

## 3. Màquina d'estats per agent (W1)

### 3.1 Estats i transicions

```
                     ┌──────────────────────────────────────────┐
                     │                                          ▼
  [idle] ──missatge──► [listening] ──handraise?──► [evaluating] ─┬─► [speaking] ──torn acaba──► [idle]
                     │                                │          │
                     │                                └─► [pass] ─┘
                     │
                     └──error──► [down] ──reintent 5min──► [listening]
```

| Estat        | Significat                                             | UI (barra d'agents) |
|---|---|---|
| `idle`       | Esperant, sense missatge nou                           | Gris, icona ·        |
| `listening`  | Ha rebut el missatge, està processant el hand-raise    | Blau, 🎧             |
| `evaluating` | Hand-raise en curs (esperant resposta del model)       | Blau giratori, 🤔   |
| `will_intervene` | Ha dit que vol parlar, a la cua                    | Verd, ✋              |
| `pass`       | Ha dit que no vol parlar                               | Gris, ⏭              |
| `speaking`   | Torn actiu: generant resposta                          | Accent, 💬 + cronòmetre |
| `down`       | Caigut (quota, error, timeout)                         | Vermell, 🔻 + motiu   |

### 3.2 Publicació d'estats via socket

Els canvis d'estat s'emeten via l'envelope `collab_event.v1` (B7):

```json
{
  "type": "collab_event.v1",
  "seq": 42,
  "channel_id": "...",
  "event": {
    "type": "agent_state",
    "agent_id": "codex-sol",
    "state": "evaluating",
    "detail": "preguntant si vol intervenir",
    "timestamp": 1790000000
  }
}
```

El frontend manté un store `collabState` alimentat per push. En reconnect, demana re-sync des
de l'últim `seq` rebut (`GET /collab/events?since=<seq>`).

---

## 4. Escolta garantida — W9 (`collab_receipt`)

### 4.1 Flux quan entra un missatge humà

1. **Missatge rebut** → es publica com a `collab_event` type=`user_message`.
2. **Per cada agent** → es crea `collab_receipt` amb state=`received`.
3. **Immediatament** → cada agent passa a `listening` (canvi visible a la barra).
4. **Hand-raise** → quan l'agent comença el hand-raise, el seu receipt passa a `evaluating`.
5. **Resposta del hand-raise**:
   - `intervene: true` → receipt passa a `will_intervene` + agent passa a `will_intervene`.
   - `intervene: false` → receipt passa a `pass` + agent torna a `idle`.
6. **Resum visible** al frontend: `Rebut per 3/3 · 2 valorant · 1 vol intervenir`.
7. **Si ningú vol intervenir** → avís explícit: «Cap agent vol intervenir. Escriu més context o
   reactiva amb /collab start.»

### 4.2 Missatge humà durant una ronda activa

El missatge no s'ignora fins a la propera ronda. Amb W10, el missatge genera un event
`user_message` que **invalida els handraises actius** (superseded) i força una reavaluació
abans del torn següent. Sense W10 (mode `rounds`), el missatge queda al context i es processa
a la propera volta de hand-raise (comportament actual).

### 4.3 Socket: `collab:receipt_updated`

Cada transició d'estat d'un receipt s'emet per socket perquè el frontend actualitzi el resum en
temps real:

```json
{
  "type": "collab:receipt_updated",
  "channel_id": "...",
  "event_seq": 42,
  "agent_id": "claude-fable",
  "state": "will_intervene",
  "summary": { "received": 3, "evaluating": 0, "will_intervene": 2, "pass": 1 }
}
```

---

## 5. Scheduler continu — W10

### 5.1 Mode `rounds` vs `continuous`

Definit per `conversation_mode` (W13), independent de `speaker_policy`:

- **`rounds`** (per defecte, comportament actual): una volta de hand-raise per missatge humà;
  si ningú vol parlar, silenci o empenta. Sense preempció.
- **`continuous`** (nou): el scheduler processa esdeveniments en temps real; un missatge humà
  pot invalidar handraises pendents i reordenar la cua de torns sense esperar cap ronda.

### 5.2 Bucle del scheduler continu

```
┌─────────────────────────────────────────────────────────────┐
│  Worker adquireix lease de collab_session                    │
│                                                              │
│  LOOP:                                                       │
│    1. Llegir events actius des de last_event_seq             │
│    2. Processar per ordre de seq:                            │
│       a. user_message → crear receipts, invalidate           │
│          handraises anteriors (superseded), avaluar torns    │
│       b. handraise → afegir/actualitzar a la cua             │
│       c. turn_end → alliberar agent, reavaluar cua          │
│       d. cancel → marcar torn actual com aturat             │
│    3. Si hi ha voluntaris a la cua i cap torn actiu:         │
│       → iniciar torn del primer per prioritat               │
│    4. Renovar lease (cada 10s)                               │
│    5. Si no hi ha events ni torn actiu per 60s:              │
│       → session.idle, alliberar lease                        │
│    6. await asyncio.sleep(0.5) — bucle lleuger               │
│                                                              │
│  EXCEPCIÓ: si el lease es perd (TTL expirat), sortir net     │
└─────────────────────────────────────────────────────────────┘
```

### 5.3 Política d'interrupció (preempció)

Quan un missatge humà arriba durant un torn actiu (mode `continuous`):

| Fase del torn actual         | Acció                                          |
|---|---|
| `queued` (a la cua)          | Cancel·lar immediatament                       |
| `generating` (model activat) | Cancel·lar la generació (`run_id` cancel·lable) |
| `streaming` (responent)      | Aturar si possible; deixar acabar la frase     |
| `tool/file` (executant tool) | Acabar la unitat atòmica, després cedir        |

**Mai** s'inicia un torn nou sense confirmar que l'anterior ha acabat (estat terminal).

### 5.4 Invalidació de handraises

```python
async def invalidate_pending_handraises(channel_id: str, before_seq: int):
    """Marca com superseded tots els handraises actius anteriors a before_seq.
    Idempotent: superseded és terminal."""
    async with get_async_db_context() as session:
        await session.execute(text("""
            UPDATE collab_event
            SET status = 'superseded'
            WHERE channel_id = :cid
              AND seq < :N
              AND type = 'handraise'
              AND status = 'active'
        """), {"cid": channel_id, "N": before_seq})
        await session.commit()
```

---

## 6. Integració amb `orchestrator.py` — canvis concrets

### 6.1 Reemplaçar `_active_rounds` per `collab_session`

Avui: `_active_rounds: dict[str, dict] = {}` — estat en memòria, perdut en reiniciar.

Nou: la funció `run_round()` adquireix el lease de `collab_session`. Si ja hi ha una sessió
activa (un altre worker o una ronda prèvia), retorna immediatament.

```python
async def _acquire_lease(channel_id: str, worker_id: str, ttl: int = 30) -> bool:
    async with get_async_db_context() as session:
        result = await session.execute(text("""
            UPDATE collab_session
            SET lease_owner = :wid, lease_expires_at = :now + :ttl, updated_at = :now
            WHERE channel_id = :cid
              AND (lease_owner = :wid OR lease_expires_at < :now OR lease_owner IS NULL)
        """), {"wid": worker_id, "ttl": ttl, "now": int(time.time()), "cid": channel_id})
        await session.commit()
        return result.rowcount > 0
```

### 6.2 Hooks de telemetria (ja dissenyats, esperant integració)

Els punts d'integració de `record_usage()` (W15 Capa 1) a l'orquestrador:

| Funció               | Punt exacte                              | Dades a capturar                        |
|---|---|---|
| `_quick_completion`  | Després de `generate_chat_completion`    | `input_tokens`, `output_tokens`, `status`, `call_type` |
| `agent_turn`         | Després que `done=True`                  | `total_tokens`, `status`, `error_detail`, `call_type="turn"` |
| `_vote_on_proposal`  | Després de cada `vote_one`               | `call_type="vote"`, `status`            |
| `_update_summary`    | Després de `_quick_completion`           | `call_type="summary"`, `status`         |
| `_mark_agent_down`   | Quan es classifica l'error               | `error_detail`, `status` (de `classify_error`) |

Claude Fable és el responsable d'aquests hooks (no trepitjar `orchestrator.py` alhora que Codex).

### 6.3 Publicació d'estats (nou, W1)

Funció nova que substitueix els placeholders `"⏳ treballant…"` per estats rics:

```python
async def _emit_agent_state(
    channel_id: str, agent_id: str, state: str, detail: str = ""
):
    """Publica un canvi d'estat d'agent com a collab_event i l'emet per socket."""
    event_id = str(uuid.uuid4())
    now = int(time.time())
    async with get_async_db_context() as session:
        seq_result = await session.execute(text("""
            SELECT COALESCE(MAX(seq), 0) + 1 FROM collab_event WHERE channel_id = :cid
        """), {"cid": channel_id})
        new_seq = seq_result.scalar()
        await session.execute(text("""
            INSERT INTO collab_event (id, channel_id, seq, type, agent_id, message_id, payload, status, created_at)
            VALUES (:id, :cid, :seq, 'agent_state', :aid, NULL, :payload, 'active', :now)
        """), {
            "id": event_id, "cid": channel_id, "seq": new_seq,
            "aid": agent_id,
            "payload": json.dumps({"state": state, "detail": detail}),
            "now": now,
        })
        await session.commit()

    # Emissió per socket
    await sio.emit("collab_event", {
        "type": "collab_event.v1",
        "seq": new_seq,
        "channel_id": channel_id,
        "event": {"type": "agent_state", "agent_id": agent_id, "state": state, "detail": detail, "timestamp": now}
    }, to=f"channel:{channel_id}")
```

### 6.4 Creació de receipts (nou, W9)

```python
async def _create_receipts(channel_id: str, event_seq: int, agents: list[str]):
    """Crea un collab_receipt 'received' per cada agent quan entra un missatge humà."""
    now = int(time.time())
    async with get_async_db_context() as session:
        for agent_id in agents:
            await session.execute(text("""
                INSERT OR IGNORE INTO collab_receipt (id, event_seq, channel_id, agent_id, state, updated_at)
                VALUES (:id, :seq, :cid, :aid, 'received', :now)
            """), {
                "id": str(uuid.uuid4()), "seq": event_seq,
                "cid": channel_id, "aid": agent_id, "now": now,
            })
        await session.commit()

        # Emissió del resum inicial
        await _emit_receipt_summary(channel_id, event_seq)
```

---

## 7. Migració incremental (4 passos del pla)

El pla defineix 4 passos per migrar de rondes a scheduler sense trencar res:

| Pas | Què                                    | Risc                    | Reversió        |
|---|---|---|---|
| 1   | Crear les 3 taules (`collab_session`, `collab_event`, `collab_receipt`) | Cap — només DDL | DROP TABLE |
| 2   | Adquirir lease persistent abans de `run_round` (en comptes de `_active_rounds`) | Si el lease falla, la ronda no arrenca | Flag de feature toggle |
| 3   | Preempció del missatge humà (W10 continuous mode) | Torns interromputs | Desactivar `continuous` → torna a `rounds` |
| 4   | Event loop estructural (W10 complet, Fase 6) | Refactor gran | Mantenir `rounds` com a fallback permanent |

Cada pas es pot provar independentment i revertir sense afectar els anteriors.

---

## 8. Frontend (W1 + W9)

### 8.1 `CollabAgentsBar.svelte` (nou)

Barra horitzontal al capçal del canal, sempre visible (no cal obrir el panell):

```
┌─────────────────────────────────────────────────────────────────┐
│  🤝 Codex Sol 🎧  │  🧠 Claude Fable ✋  │  ⚡ Z.ai 🤔  │  👤 Esteve │
│  escoltant          vol parlar              valorant               │
└─────────────────────────────────────────────────────────────────┘
```

- Store `collabState` (Svelte writable) alimentat per socket `collab_event`.
- Cada agent: avatar/inicials + nom + icona d'estat + cronòmetre si `speaking`.
- Color d'accent per agent (connecta amb W14).
- Caiguts en vermell amb motiu abreujat.
- Re-sync en reconnect: `GET /collab/events?since=<last_seq>`.

### 8.2 Franja W9 sota cada missatge humà

```
┌─────────────────────────────────────────────────────────┐
│ [Esteve]: el que he demanat                              │
│ ─────────────────────────────────────────────────────── │
│  📨 Rebut per 3/3 · 2 valorant · 1 vol intervenir       │
│  [▼ Detall per agent]                                    │
│    Codex Sol: ✋ vol intervenir (prioritat 4)            │
│    Claude Fable: 🤔 valorant                             │
│    Z.ai: ⏭ passa                                         │
└─────────────────────────────────────────────────────────┘
```

Desplegable col·lapsable (no amaga missatges — és metadada visual sobre el missatge existent).

### 8.3 Feedback W10

```
⚡ Missatge prioritari rebut — reavaluant la cua de torns…
```

Apareix sobre la barra d'agents quan un missatge humà invalida handraises.

---

## 9. Envelope d'esdeveniments `collab_event.v1` (B7)

Tots els esdeveniments (agent_state, user_message, agent_message, handraise, turn_start,
turn_end, cancel, receipt_updated) s'embolcallen amb el mateix format:

```typescript
interface CollabEvent {
  type: "collab_event.v1";      // versió del contracte
  seq: number;                    // monòton per canal (per detectar buits)
  channel_id: string;
  event: {
    type: string;                 // tipus concret
    agent_id?: string;
    message_id?: string;
    state?: string;               // per agent_state
    detail?: string;
    summary?: ReceiptSummary;     // per receipt_updated
    timestamp: number;
  };
}
```

El frontend manté `last_seq` i el fa servir per al re-sync. Si `seq` salta, demana els events
perduts.

---

## 10. Criteris d'acceptació

### W1 — Visibilitat
- [ ] La barra d'agents mostra l'estat de cada agent en temps real (push, no polling).
- [ ] L'estat d'un agent canvia en ≤ 1s des del backend.
- [ ] Desconnexió i reconnect: el frontend recupera l'estat complet via re-sync.
- [ ] Agent caigut apareix en vermell amb motiu.
- [ ] Cronòmetre visible durant torns actius.

### W9 — Escolta garantida
- [ ] Cada missatge humà genera un receipt per agent en ≤ 3s.
- [ ] El resum `Rebut per X/N · Y valorant · Z vol intervenir` s'actualitza en temps real.
- [ ] Si ningú vol intervenir, hi ha un avís explícit (mai silenci sense explicació).
- [ ] Missatge a mig torn (mode rounds) queda al context i es processa la propera volta.
- [ ] Desplegable per agent mostra l'estat individual de cada receipt.

### W10 — Conversa fluida
- [ ] Mode `continuous`: un missatge humà invalida handraises anteriors i reordena la cua.
- [ ] Seq monòton sense buits amb 50 insercions concurrents.
- [ ] Un sol lease holder per canal; TTL expirat → un altre worker recull.
- [ ] Invalidació `superseded` és idempotent (10 execucions = mateix resultat).
- [ ] Operacions amb efectes (tools, fitxers) no queden parcialment executades per interrupció.
- [ ] Mode `rounds` conserva exactament el comportament actual (no-regressió).

---

## 11. Coordinació amb l'equip

- **Codex Sol:** responsable principal de `orchestrator.py` + migració de les 3 taules +
  scheduler.
- **Claude Fable:** hooks de telemetria a `orchestrator.py` (coordinar per no editar alhora) +
  `CollabAgentsBar.svelte` + franja W9 al frontend.
- **Z.ai.glm-5.2:** aquest disseny + revisió de concurrència + proves de no-regressió.
- **Frontend agent (si l'Esteve l'afegeix):** `CollabAgentsBar.svelte`, re-sync, store Svelte.

**Ordre d'edició d'`orchestrator.py`:** només un agent l'edita alhora. Protocol:
1. Anunciar al xat «agafo orchestrator.py per X».
2. Els altres esperen.
3. Anunciar «alliberat» quan s'acabi.

---

## 12. Riscos i mitigacions

| Risc | Mitigació |
|---|---|
| Lease que no es renova (worker penjat) | TTL 30s + detecció automàtica; un altre worker recull |
| Event log creix indefinidament | Purge d'events `consumed` + `superseded` > 24h (job periòdic) |
| Preempció de tool amb efectes parcials | Política escalonada: tool/file acaba la unitat atòmica abans de cedir |
| Mode `continuous` massa agressiu (interromp constantment) | `speaker_policy` + cooldown entre interrupcions (mínim 1 torn complet abans de re-preemptar) |
| Socket disconnect durant torn | Re-sync per `seq`; l'estat dels agents es reconstrueix des de `collab_event` |
