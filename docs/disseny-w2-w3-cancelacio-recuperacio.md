# Disseny W2/W3 — Cancel·lació segura i ronda única recuperable

> Autor: Z.ai.glm-5.2 · Data: 17/07/2026
> Dependències: motor W1/W9/W10 (`engine.py`, `collab_session`, `collab_event`, `collab_receipt`)
> Coordinació: Codex Sol (orquestrador); Claude Fable (frontend)
> W2 resol: B1 (timeout no cancel·la generació), F2 (done per polling), B3 (aturada cooperativa),
>           F4 (no es pot tallar un torn des de la UI)
> W3 resol: B2 (exclusió de rondes només en memòria), B4 (recuperació post-reinici), B8 (missatge humà espera ronda)

---

## 1. Objectiu

Avui `orchestrator.py` té tres febleses estructurals:

1. **El timeout no cancel·la la generació** — `turn_timeout` talla el polling però el procés del model
   segueix corrent (tokens, cost, recursos). No hi ha `run_id` cancel·lable. (B1)
2. **La detecció de fi de torn és per polling cada 1.5s** — llatenta, fràgil i penjada per sempre si
   `turn_timeout=0`. (F2)
3. **L'aturada és cooperativa** — `request_stop()` marca un flag que només es comprova entre torns.
   No es pot tallar un torn en curs des de la UI. (B3+F4)
4. **L'exclusió de rondes és en memòria** (`_active_rounds: dict`) — es perd en reiniciar, permet
   dues rondes si hi ha múltiples workers, i no es reconcilia post-crash. (B2)
5. **No hi ha recuperació** — un reinici a mig ronda deixa la sessió en estat inconsistent sense
   reparació. (B4)

W2 resol 1-3 (cancel·lació), W3 resol 4-5 (ronda única recuperable).

---

## 2. W2 — Cancel·lació i timeout reals

### 2.1 Arquitectura: `turn_id` + `asyncio.CancelledError`

Cada torn té un identificador únic (`turn_id`) que permet cancel·lar-lo des de qualsevol lloc
(UI, timeout, missatge humà prioritzat). La cancel·lació es propaga com `asyncio.CancelledError`
al coroutine que executa la generació.

```python
# Nous camps a l'estat de torn (al scheduler o a run_round)
_turn_tasks: dict[str, asyncio.Task] = {}   # turn_id → Task en curs
_turn_cancellables: dict[str, dict] = {}     # turn_id → {channel_id, agent_id, run_id, started_at}
```

### 2.2 Punt d'entrada: `cancel_turn(turn_id, reason)`

```python
async def cancel_turn(turn_id: str, reason: str = "user_requested") -> bool:
    """Cancel·la un torn en curs. Retorna True si s'ha pogut cancel·lar.
    
    Motius: 'user_requested' (botó UI), 'timeout' (turn_timeout), 
    'preempted' (missatge humà prioritzat en mode continuous), 
    'agent_down' (caiguda detectada).
    """
    task = _turn_tasks.get(turn_id)
    if task is None or task.done():
        return False
    
    # Marcar el motiu abans de cancel·lar (per al missatge al canal)
    info = _turn_cancellables.get(turn_id, {})
    info["cancel_reason"] = reason
    _turn_cancellables[turn_id] = info
    
    task.cancel()
    return True
```

### 2.3 Integració amb `agent_turn`

La funció `agent_turn` actual fa dues coses que cal protegir:

1. `CHAT_COMPLETION_HANDLER(request, form_data, user=user)` — crida de generació
2. `while True: await asyncio.sleep(1.5)` — polling de `meta.done`

**Nou patró: la crida de generació és un Task cancel·lable.**

```python
async def agent_turn(request, channel, config, user, models, agent_id, nudge=None):
    turn_id = str(uuid.uuid4())
    
    # ... (crear placeholder, construir system + prompt) ...
    
    # Crear el Task de generació (cancel·lable)
    gen_task = asyncio.create_task(
        request.app.state.CHAT_COMPLETION_HANDLER(request, form_data, user=user)
    )
    _turn_tasks[turn_id] = gen_task
    _turn_cancellables[turn_id] = {
        "channel_id": channel.id,
        "agent_id": agent_id,
        "started_at": time.time(),
    }
    
    try:
        # Esperar la generació O el timeout O la cancel·lació
        timeout = int(config.guardrail("turn_timeout") or 0) or None
        # En lloc de polling cada 1.5s, esperar la tasca amb timeout
        await (asyncio.wait_for(gen_task, timeout) if timeout else gen_task)
    except asyncio.TimeoutError:
        gen_task.cancel()
        await post_notice(..., f"⏱️ Torn de {name} tallat per timeout ({timeout}s).")
    except asyncio.CancelledError:
        reason = _turn_cancellables.get(turn_id, {}).get("cancel_reason", "cancelled")
        # Propagar la cancel·lació al procés del model si és possible
        _notify_model_cancellation(channel.id, form_data, reason)
        # NO re-raise: la ronda continua sense aquest torn
    finally:
        _turn_tasks.pop(turn_id, None)
        _turn_cancellables.pop(turn_id, None)
    
    # ... (detecció de canvis, telemetria, etc.) ...
```

### 2.4 Canvi fonamental: fi de torn per senyal, no per polling

Avui el polling cada 1.5s és l'únic senyal de fi de torn. El nou patró:

1. **El `CHAT_COMPLETION_HANDLER` ja marca `meta.done=True`** quan la generació acaba
   (el middleware del chat ho fa internament). Però a `agent_turn` no hi esperàvem directament.
2. **Amb el nou patró**, `await gen_task` bloca fins que la generació acaba naturalment.
   No cal polling.
3. **Si el handler no suporta cancel·lació cooperativa** (p.ex. un pipe CLI que executa un procés),
   la cancel·lació de l'`asyncio.Task` allibera el corredor però el procés del CLI pot seguir.
   En aquest cas, el que fem és:
   - Marcar el missatge placeholder com a cancel·lat (`meta.done=True`, `meta.cancelled=True`)
   - Deixar que el procés acabi per compte propi (el CLI retornarà quan acabi, i el handler
     marcarà `done` sobre un missatge ja tancat — idempotent)

### 2.5 Cancel·lació segura d'operacions amb efectes

La política escalonada de W10 (disseny-w1-w9-w10-motor.md §5.3) és crítica aquí:

| Fase del torn actual | Acció de cancel·lació |
|---|---|
| `queued` (a la cua) | Cancel·lar immediatament — no hi ha efectes |
| `generating` (model activat, abans de tool calls) | Cancel·lar el Task; el model no ha fet efectes |
| `streaming` (responent al canal) | Aturar si possible; el missatge queda amb el que ja ha sortit |
| `tool_executing` (executant write_project_file, propose_finish...) | **ESPERAR** a que l'operació atòmica acabi; marcar com a `cancel_pending` |

**Implementació de la fase `tool_executing`:**

El pipe/middleware pot emetre un senyal `collab:tool_lock` quan comença una operació amb efectes:

```python
# Al middleware (o al tool handler):
# Abans de write_project_file:
await sio.emit("collab:tool_lock", {"turn_id": turn_id, "channel_id": channel_id, "tool": "write_project_file"})
# Després:
await sio.emit("collab:tool_unlock", {"turn_id": turn_id})
```

El cancel·lador respecta el lock:

```python
async def cancel_turn(turn_id: str, reason: str) -> bool:
    info = _turn_cancellables.get(turn_id)
    if info and info.get("tool_locked"):
        info["cancel_pending"] = True  # cancel·larà quan s'alliberi el lock
        return False  # encara no cancel·lat
    # ... cancel·lar ...
```

### 2.6 Endpoint REST: `POST /collab/turn/cancel`

```python
@router.post("/collab/channels/{channel_id}/turn/cancel")
async def cancel_channel_turn(channel_id: str, user=Depends(get_verified_user)):
    """Cancel·la el torn actiu del canal (botó ✖ de la UI)."""
    # Trobar el turn_id actiu del canal
    for tid, info in _turn_cancellables.items():
        if info.get("channel_id") == channel_id:
            cancelled = await cancel_turn(tid, "user_requested")
            if cancelled:
                return {"status": "cancelled", "turn_id": tid}
    return {"status": "no_active_turn"}
```

### 2.7 Timeout de seguretat no desactivable

Avui `turn_timeout=0` vol dir "sense límit" → un agent penjat bloqueja la ronda per sempre.

**Nova regla:** sempre hi ha un `max_turn_hard_timeout` (per defecte 600s = 10 minuts) que no es
pot desactivar via guardarails. Si `turn_timeout=0`, el hard timeout de 600s s'aplica igualment.

```python
MAX_TURN_HARD_TIMEOUT = 600  # no configurable a baix

async def agent_turn(...):
    configured = int(config.guardrail("turn_timeout") or 0)
    effective_timeout = max(configured, MAX_TURN_HARD_TIMEOUT) if configured else MAX_TURN_HARD_TIMEOUT
    # ...
```

### 2.8 Senyal de fi estructurat (F2)

En lloc de polling, el `CHAT_COMPLETION_HANDLER` o el seu middleware emet `collab:turn_done`:

```python
# Quan el middleware marca meta.done=True:
await sio.emit("collab:turn_done", {
    "type": "collab_event.v1",
    "channel_id": channel_id,
    "turn_id": turn_id,
    "message_id": message_id,
    "timestamp": int(time.time()),
})
```

L'orquestrador escolta aquest senyal per saber que el torn ha acabat sense polling:

```python
done_event = asyncio.Event()

async def _on_turn_done(data):
    if data.get("turn_id") == turn_id:
        done_event.set()

sio.on("collab:turn_done", _on_turn_done)
# ...
await asyncio.wait_for(done_event.wait(), effective_timeout)
```

> **Nota d'implementació:** el `CHAT_COMPLETION_HANDLER` actual és síncron respecte a l'orquestrador
> (el cridem amb `await`). Quan acaba, la generació ha acabat. Però amb streaming, el handler retorna
> abans que l'últim chunk s'hagi escrit al missatge. Cal coordinar amb el middleware per saber quan
> el missatge està realment complet. Alternativament, podem mantenir un polling lleuger (cada 0.5s
> en lloc de 1.5s) com a fallback mentre no es tingui el senyal estructurat.

---

## 3. W3 — Una sola ronda, recuperable

### 3.1 Reemplaçar `_active_rounds` per lease persistent

Avui: `_active_rounds: dict[str, dict]` — si el procés es reinicia, es perd. Un altre worker pot
iniciar una segona ronda concurrent.

**Nou:** la funció `run_round` adquireix el lease de `collab_session` (ja implementat a `engine.py`)
abans de començar el bucle. Si un altre worker ja el té, retorna immediatament.

```python
async def run_round(request, channel, user):
    worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    
    if not await acquire_lease(channel.id, worker_id, ttl=30):
        log.info("Ronda ja activa al canal %s (lease ocupat)", channel.id)
        return
    
    # ... bucle principal ...
    
    # Renovar lease cada 10s en paral·lel
    lease_renewal_task = asyncio.create_task(_renew_lease_loop(channel.id, worker_id))
    
    try:
        # ... bucle de torns ...
    finally:
        lease_renewal_task.cancel()
        await release_lease(channel.id, worker_id)
```

### 3.2 Renovació del lease

```python
async def _renew_lease_loop(channel_id: str, worker_id: str):
    """Renova el lease cada 10s mentre la ronda estigui activa."""
    while True:
        await asyncio.sleep(10)
        renewed = await acquire_lease(channel_id, worker_id, ttl=30)
        if not renewed:
            log.warning("Lease perdut al canal %s — un altre worker l'ha pres", channel_id)
            request_stop(channel_id)  # aturar el bucle
            return
```

### 3.3 Reconciliació post-reinici (B4)

Quan el backend arranca, pot haver-hi sessions `collab_session` amb `status='active'` i un
`lease_owner` que ja no existeix (el procés anterior ha mort). El lease té TTL 30s, o sigui que
després de 30s qualsevol worker pot adquirir-lo de nou.

**Però el problema més subtil:** la ronda que s'executava s'ha interromput a mig camí. Hi ha un
torn potencialment en curs (placeholder amb `done=False`). Cal reconciliar:

```python
async def reconcile_channel(channel_id: str, request, user):
    """Cridada en arrencar (o periòdicament) per detectar sessions actives
    amb torns orfes."""
    session = await get_collab_session(channel_id)
    if not session or session.status != "active":
        return
    
    # Buscar missatges placeholder amb done=False (torns interromputs)
    messages = await Messages.get_messages_by_channel_id(channel_id, 0, 5)
    orphans = [m for m in messages if (m.meta or {}).get("model_id") and not (m.meta or {}).get("done")]
    
    for orphan in orphans:
        # Marcar com a interromput
        meta = orphan.meta or {}
        meta["done"] = True
        meta["interrupted"] = True
        meta["interrupted_reason"] = "server_restart"
        await Messages.update_message_by_id(orphan.id, content=orphan.content, meta=meta)
        
        # Registrar event
        await append_event(
            channel_id, "turn_end",
            agent_id=meta.get("model_id"),
            payload={"reason": "server_restart", "message_id": orphan.id},
        )
    
    # Alliberar el lease stale
    await release_lease(channel_id, session.lease_owner or "", stopped=True)
    
    # Si hi havia un missatge humà recent sense processar, reiniciar la ronda
    if orphans:
        channel = await Channels.get_channel_by_id(channel_id)
        if channel:
            config = get_collab_config(channel)
            if config.enabled and config.agents:
                asyncio.create_task(run_round(request, channel, user))
```

### 3.4 El missatge humà no espera la ronda completa (B8)

Avui: `handle_collab_message` crida `run_round` síncronament. Si hi ha una ronda activa, el missatge
humà queda dins el context i es processa la propera volta de hand-raise.

**Problema:** en mode `rounds`, si la ronda té 8 torns programats, el missatge humà espera tots 8
abans de ser considerat. (B8)

**Solució per a mode `continuous` (W10):** el missatge humà genera un event `user_message` que
invalida handraises anteriors (`supersede_handraises`) i força una reavaluació immediata. Aquest
flux ja està dissenyat al motor W1/W9/W10.

**Solució per a mode `rounds` (compatibilitat):** 

Quan arriba un missatge humà durant una ronda activa:

```python
async def handle_collab_message(request, channel, message, user):
    # ... (comprovacions existents) ...
    
    if is_round_active(channel.id) or await _has_active_lease(channel.id):
        # Hi ha ronda activa. En mode rounds, el missatge entra al context
        # i es processa la propera volta. En mode continuous, invalida.
        config = get_collab_config(channel)
        if config.mode == "continuous":
            await append_event(channel.id, "user_message", message_id=message.id)
            await supersede_handraises(channel.id, before_seq=_latest_seq(channel.id))
            # El scheduler detectarà el nou event i reavaluarà
        else:
            # Mode rounds: el missatge es processa a la propera volta
            # (comportament actual, sense canvis)
            pass
        return True
    
    await run_round(request, channel, user)
    return True
```

### 3.5 Detecció de Lease perdut → aturada neta

Si el worker perd el lease (un altre worker l'ha pres, o el TTL ha expirat i algú l'ha agafat),
la ronda en curs ha d'aturar-se de manera neta:

```python
# Dins del bucle principal de run_round:
if not await acquire_lease(channel.id, worker_id, ttl=30):
    # Lease perdut
    log.warning("Lease perdut durant la ronda del canal %s — aturant", channel.id)
    break
```

Aquesta comprovació es fa a cada iteració del bucle (cada volta de hand-raise). A més, el
`_renew_lease_loop` pot forçar l'aturada si la renovació falla.

---

## 4. Migració de `_active_rounds` a lease persistent

### 4.1 Compatibilitat amb el codi existent

El flag `stop` d'avui (`state["stop"] = True`) es manté com a senyal cooperativa dins del bucle,
però l'exclusió real es fa amb el lease. La transició és:

```python
# Abans:
def is_round_active(channel_id: str) -> bool:
    return channel_id in _active_rounds

def request_stop(channel_id: str) -> bool:
    state = _active_rounds.get(channel_id)
    if state:
        state["stop"] = True
        return True
    return False

# Després:
def is_round_active(channel_id: str) -> bool:
    return channel_id in _active_rounds  # encara útil per al flag stop

def request_stop(channel_id: str) -> bool:
    state = _active_rounds.get(channel_id)
    if state:
        state["stop"] = True
        return True
    return False

# Dins de run_round:
async def run_round(request, channel, user):
    worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    
    # Exclusió real amb lease persistent
    if not await acquire_lease(channel.id, worker_id, ttl=30):
        return  # un altre worker té el lock
    
    state = {"stop": False}
    _active_rounds[channel.id] = state  # per al flag stop local
    try:
        # ... bucle amb renovació de lease ...
    finally:
        _active_rounds.pop(channel.id, None)
        await release_lease(channel.id, worker_id)
```

### 4.2 Eliminació progressiva de `_active_rounds`

Un cop el lease estigui provat, `_active_rounds` només serveix per al flag `stop` local. Es pot
migrar a una variable dins del bucle sense dict global:

```python
async def run_round(request, channel, user):
    worker_id = ...
    if not await acquire_lease(channel.id, worker_id, ttl=30):
        return
    
    stop_flag = {"stop": False}  # local, no global
    
    # request_stop accedeix a aquest flag via una referència...
    # O millor: usar un asyncio.Event que es pot set() des de fora
```

> **Decisió:** mantenir `_active_rounds` com a mecanisme de flag `stop` local durant la Fase 2
> del pla. Eliminar-lo completament a la Fase 6 (event loop estructural), quan el scheduler
> substitueixi `run_round` del tot.

---

## 5. Flux complet d'un torn amb cancel·lació

```
┌─────────────────────────────────────────────────────────────────────┐
│  run_round adquireix lease (collab_session)                          │
│                                                                      │
│  ┌─ handraise → voluntaris → speaker = voluntari[0]               │
│  │                                                                │
│  │  ┌─ agent_turn(speaker)                                       │
│  │  │  1. Crear placeholder "⏳ treballant…"                     │
│  │  │  2. turn_id = uuid4()                                      │
│  │  │  3. gen_task = create_task(CHAT_COMPLETION_HANDLER)        │
│  │  │  4. Registrar turn_id a _turn_tasks                        │
│  │  │  5. await gen_task  ←  bloqueja aquí                        │
│  │  │                                                            │
│  │  │     ╌╌╌╌ [PARAL·LEL] ╌╌╌╌                                  │
│  │  │     • POST /turn/cancel → task.cancel()                    │
│  │  │     • turn_timeout → wait_for cancel·la                    │
│  │  │     • _renew_lease falla → request_stop                    │
│  │  │     ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌          │
│  │  │                                                            │
│  │  │  6. try:                                                   │
│  │  │       await wait_for(gen_task, effective_timeout)          │
│  │  │     except TimeoutError:                                   │
│  │  │       cancel + avís ⏱️                                      │
│  │  │     except CancelledError:                                 │
│  │  │       avís ✖ (motiu: user/timeout/preempt/down)            │
│  │  │     finally:                                               │
│  │  │       netejar _turn_tasks                                  │
│  │  │                                                            │
│  │  │  7. Detecció de canvis, telemetria, _mark_agent_up/down    │
│  │  └────────────────────────────────────────────────────────────┘
│  │                                                                │
│  └─ next: another handraise or stop                              │
│                                                                      │
│  release_lease                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 6. Reconciliació d'orfes: escenaris

| Escenari | Estat deixat | Detecció | Acció |
|---|---|---|---|
| Backend reiniciat a mig torn | `placeholder.done=False` + `session.status=active` + lease stale | `reconcile_channel()` en arrencada | Marcar placeholder com `interrupted`, alliberar lease, reprendre si cal |
| Worker perd lease (un altre l'ha pres) | Lease canvia de owner | `_renew_lease_loop` falla | `request_stop()` local → bucle surt netament |
| Torn cancel·lat per l'usuari | `gen_task` cancel·lat | `CancelledError` a `agent_turn` | Avís ✖ al canal, marcar missatge com `cancelled=True`, continuar |
| Timeout hard (600s) | `gen_task` cancel·lat per `wait_for` | `TimeoutError` | Avís ⏱️, marcar agent `down` si no ha produït res |
| Procés del CLI segueix corrent post-cancel | Recursos del procés CLI | (No detectable sense PID) | Acceptat: el CLI acabarà; els efectes ja s'han respectat via `tool_lock` |

---

## 7. Integració amb el motor W1/W9/W10

W2/W3 no són independents del motor — en formen part:

- **El lease de `collab_session`** (W3) ja està implementat a `engine.py` (`acquire_lease`,
  `release_lease`). Només cal cridar-lo des de `run_round`.
- **La cancel·lació de torns** (W2) és un nou mecanisme que s'insereix dins de `agent_turn`.
- **La reconciliació d'orfes** (W3) és una funció nova que es crida en arrencar.
- **La preempció del missatge humà** (W10) usa `cancel_turn(turn_id, "preempted")` per
  interrompre el torn actiu quan un missatge humà arriba en mode `continuous`.

### Ordre d'implementació recomanat

1. **W3-A: Lease a `run_round`** — canvi petit: substituir el guard `channel.id in _active_rounds`
   per `acquire_lease`. Provable aïlladament.
2. **W3-B: Reconciliació** — funció `reconcile_channel` + crida en arrencada. Provable reiniciant
   el backend a mig ronda.
3. **W2-A: `turn_id` + `cancel_turn`** — estructurar `agent_turn` amb Task cancel·lable.
4. **W2-B: Endpoint REST `/turn/cancel`** — botó UI.
5. **W2-C: `tool_lock`** — protecció d'operacions amb efectes. Més complex; es pot fer al final.
6. **W2-D: Timeout hard no desactivable** — 1 línia de codi.

---

## 8. Criteris d'acceptació

### W2 — Cancel·lació i timeout reals

- [ ] `POST /collab/channels/:id/turn/cancel` atura la generació en curs en ≤ 2s (excepte si hi ha un
  `tool_lock` actiu, cas en què s'atura quan s'allibera).
- [ ] El timeout cancel·la la generació (no només el polling): el procés del model rep la cancel·lació.
- [ ] `turn_timeout=0` no penja la ronda: el hard timeout de 600s s'aplica.
- [ ] Un torn interromput deixa el missatge placeholder marcat amb `meta.cancelled=True` i `meta.done=True`.
- [ ] Les operacions amb efectes (`write_project_file`, `propose_finish`) no queden a mig executar.
- [ ] El motiu de la cancel·lació és visible al canal: `✖ {agent} tallat (motiu: user/timeout/preempt/down)`.

### W3 — Una sola ronda, recuperable

- [ ] Dos workers que intenten `run_round` sobre el mateix canal: només un comença (l'altre rep
  `acquire_lease → False`).
- [ ] TTL expirat: després de 30s sense renovació, un altre worker pot adquirir el lease.
- [ ] Reinici del backend a mig ronda: els placeholders orfes es marquen com `interrupted`, el lease
  s'allibera, i la ronda es pot reprendre.
- [ ] Missatge humà durant ronda activa en mode `rounds`: queda al context (comportament actual).
- [ ] Missatge humà durant ronda activa en mode `continuous`: invalida handraises i força reavaluació.
- [ ] `_renew_lease_loop`: si la renovació falla, la ronda surt del bucle netament.

---

## 9. Riscos i mitigacions

| Risc | Mitigació |
|---|---|
| Cancel·lar un `asyncio.Task` no atura el procés subjacent (CLI) | Acceptat: el CLI acabarà; els efectes es respecten via `tool_lock` |
| `tool_lock` no implementat pels pipes | Fase 1 sense `tool_lock`: la cancel·lació pot interrompre a mig tool call → registrar com a limitació coneguda fins que es implementi |
| Lease renewat massa tard (race amb TTL) | TTL 30s + renew cada 10s → marge de 20s; acceptable |
| `reconcile_channel` executa abans que la BD estigui llesta | Cridar-la després de `init_db()` al startup |
| Dos workers treballant alhora (race inicial) | `acquire_lease` és atòmic (compare-and-set via UPDATE … WHERE … RETURNING); només un guanya |
| Procés CLI cancel·lat deixa arxius a mig escriure | `tool_lock` (fase posterior); el pipe CLI ja usa transaccions/quoting |
| `asyncio.CancelledError` es menja per un except genèric | Sempre `except asyncio.CancelledError` abans de `except Exception` |

---

## 10. Coordinació amb l'equip

- **Codex Sol:** responsable principal d'implementar W2 i W3 a `orchestrator.py`. Aquest disseny
  és la guia. El motor `engine.py` ja té `acquire_lease`/`release_lease` llestos.
- **Claude Fable:** botó ✖ «talla» al frontend (`CollabAgentsBar.svelte` o al xip de torn actiu),
  i handler per al senyal `collab:turn_done` (F2).
- **Z.ai.glm-5.2:** aquest disseny + revisió de concurrència del lease i la cancel·lació + proves
  de no-regressió (mode `rounds` ha de conservar el comportament actual).
- **Qwen local:** revisió de la lògica de reconciliació d'orfes (casos límit) i tests.

**Ordre d'edició d'`orchestrator.py`:** només un agent l'edita alhora (regla establerta).
