# Especificacions de proves — W1/W9/W10 (Motor persistent)

> Autor: Z.ai.glm-5.2 · Data: 17/07/2026
> Coberta: disseny `disseny-w1-w9-w10-motor.md` + implementació actual a `engine.py` + `orchestrator.py`.
>
> Estat dels tests existents:
> - `test_collab_engine.py`: 9 tests (monotonicitat d'events, idempotència de receipts, exclusivitat de lease, atomicitat de `record_user_message`, resums agregats, lease expirat, 50 insercions concurrents, transicions repetides, deduplicació de missatges humans).
> - `test_collab_orchestrator_events.py`: 7 tests (envelope `collab_event.v1`, emissió socket, handraise per prioritat, self-reply skip, roundrobin complet, pèrdua de lease neta, endpoints REST).
>
> Aquest document defineix els tests **pendents** per tancar W1/W9/W10 completament.

---

## Infraestructura de test comuna

Tots els tests comparteixen aquestes fixtures (es poden extreure a `conftest.py`):

### Fixtures

```python
import asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from open_webui.collab.engine import CollabSession, CollabEvent, CollabReceipt

async def make_engine(tmp_path):
    """Crea una BD SQLite en memòria amb les 3 taules collab."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'collab.db'}")
    async with engine.begin() as conn:
        for table in (CollabSession.__table__, CollabEvent.__table__, CollabReceipt.__table__):
            await conn.run_sync(table.create)
    return engine

def make_sessions(engine):
    return async_sessionmaker(engine, expire_on_commit=False)
```

### Mock de `generate_chat_completion`

```python
class FakeCompletion:
    """Simula generate_chat_completion amb resposta controlable."""
    def __init__(self, responses: dict[str, str]):
        self.responses = responses  # agent_id -> content
        self.calls = []

    async def __call__(self, request, form_data, user, **kwargs):
        agent_id = form_data["model"]
        self.calls.append(agent_id)
        content = self.responses.get(agent_id, "")
        return {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
```

### Mock de `sio.emit`

```python
@pytest.fixture
def captured_emissions():
    """Llista on es capturen totes les emissions socket."""
    emissions = []
    async def fake_emit(name, payload, *, to):
        emissions.append({"name": name, "payload": payload, "room": to})
    return emissions, fake_emit
```

---

## Grup A — Transicions dels 7 estats d'agent (W1)

### A1. `idle → listening → evaluating → will_intervene → speaking → idle`

**Descripció:** Cicle complet d'un agent que rep un missatge, decideix intervenir, fa el torn i torna a repòs.

**Pre-condicions:** Canal amb 1 agent (`a1`), missatge humà registrat.

**Passos:**
1. `record_user_message("c1", ["a1"], message_id="m1")` → receipt creat amb state `received`.
2. `_transition_receipt("c1", seq, "a1", "evaluating")` → receipt passa a `evaluating`, event `agent_state` emès.
3. `_transition_receipt("c1", seq, "a1", "will_intervene")` → receipt passa a `will_intervene`, event emès.
4. L'agent fa el torn (`agent_turn`) → detectem l'estat `speaking` (placeholder creat al canal).
5. Torn acaba → l'agent torna a `idle` (no hi ha més handraises actius).

**Asserts:**
- Després del pas 2: `receipt_summary["evaluating"] == 1`.
- Després del pas 3: `receipt_summary["will_intervene"] == 1`.
- Els events emès per socket tenen `type == "agent_state"` i `payload["state"]` correcte.
- L'event `agent_state` té `seq` monotònicament creixent.

### A2. `idle → listening → evaluating → pass`

**Descripció:** Agent que rep el missatge però decideix no intervenir.

**Passos:**
1. `record_user_message` → receipt `received`.
2. `_transition_receipt(..., "evaluating")`.
3. `_transition_receipt(..., "pass")`.

**Asserts:**
- `receipt_summary["pass"] == 1`, resta a 0.
- Event `agent_state` amb `state == "pass"` emès per socket.

### A3. Agent caigut → `down` → reintent → `listening`

**Descripció:** Agent que falla en hand-raise i es marca com caigut, després es recupera.

**Passos:**
1. Hand-raise retorna error 2 vegades consecutives.
2. `_mark_agent_down` → agent marcat com `down` a `channel.meta["collab_down"]`.
3. Passat `_RETRY_DOWN_SECONDS` (mockejat a 0), el reintent automàtic permet tornar a intentar.
4. Hand-raise reeixit → `_mark_agent_up` → agent deixa d'estar `down`.

**Asserts:**
- Després de 2 errors: agent està a `get_down_agents()`.
- Després de recuperació: agent **no** està a `get_down_agents()`.
- El receipt d'un agent caigut passa a `pass` (no es queda en `received` indefinidament).

### A4. Agent omès per cooldown (no self-reply)

**Descripció:** L'últim que ha parlat no es consulta al hand-raise si `allow_self_reply` està desactivat i hi ha més d'un agent.

**Passos:**
1. Canal amb 2 agents (`a1`, `a2`).
2. `a1` acaba de parlar (`last_speaker = "a1"`).
3. Hand-raise: `a1` no es consulta.

**Asserts:**
- `candidates == ["a2"]` (no `["a1", "a2"]`).
- Receipt de `a1` queda en `pass` (no queda en `received` indefinidament — es tanca per evitar receipts oberts).

> **Nota (correcció per observació de Codex):** La implementació actual tanca els receipts dels agents omesos com a `pass`, no els deixa en `received`. Això és correcte i coherent amb evitar receipts oberts indefinidament. Test existent que ho cobreix: `test_handraise_skips_last_speaker_and_closes_receipt`.

---

## Grup B — Receipts per cada missatge humà (W9)

### B1. Un receipt per agent per missatge

**Descripció:** Un missatge humà genera exactament N receipts (un per agent configurat).

**Passos:**
1. Canal amb 3 agents (`a1`, `a2`, `a3`).
2. `record_user_message("c1", ["a1", "a2", "a3"], message_id="m1")`.

**Asserts:**
- `receipt_summary("c1", seq)["total"] == 3`.
- `receipt_summary("c1", seq)["received"] == 3`.

### B2. Idempotència: cridar `create_receipts` dues vegades no duplica

**Descripció:** `create_receipts` amb els mateixos agents sobre el mateix `event_seq` no crea duplicats.

**Passos:**
1. `record_user_message("c1", ["a1", "a2"], message_id="m1")`.
2. `create_receipts("c1", seq, ["a1", "a2"], message_id="m1")` (segona crida).

**Asserts:**
- `receipt_summary["total"] == 2` (no 4).
- Cada agent apareix una sola vegada a `list_receipts`.

> **Cobert per:** `test_collab_engine.py::test_events_are_monotonic_and_receipts_are_idempotent`

### B3. Resum visible: `Rebut per X/N · Y valorant · Z vol intervenir`

**Descripció:** L'agregació per `state` produeix un resum correcte en cada moment.

**Passos:**
1. 3 agents, missatge humà.
2. `transition_receipt(..., "a1", "evaluating")`.
3. `transition_receipt(..., "a2", "will_intervene")`.
4. `transition_receipt(..., "a3", "pass")`.

**Asserts:**
- Després del pas 2: `{"received": 2, "evaluating": 1, "will_intervene": 0, "pass": 0, "total": 3}`.
- Després del pas 3: `{"received": 1, "evaluating": 1, "will_intervene": 1, "pass": 0, "total": 3}`.
- Després del pas 4: `{"received": 0, "evaluating": 1, "will_intervene": 1, "pass": 1, "total": 3}`.

> **Cobert per:** `test_collab_engine.py::test_receipt_summaries_are_aggregated_and_messages_are_independent`

### B4. Segon missatge humà genera receipts nous sense tocar els anteriors

**Descripció:** Dos missatges humans consecutius generen receipts independents.

**Passos:**
1. `record_user_message("c1", ["a1", "a2"], message_id="m1")` → seq=1.
2. `record_user_message("c1", ["a1", "a2"], message_id="m2")` → seq=2.

**Asserts:**
- `receipt_summary("c1", 1)["total"] == 2` (intacte).
- `receipt_summary("c1", 2)["total"] == 2`.
- Transicions sobre seq=1 no afecten seq=2 i viceversa.

> **Cobert per:** `test_collab_engine.py::test_receipt_summaries_are_aggregated_and_messages_are_independent`

### B5. Supersede de handraises anteriors en missatge nou

**Descripció:** Quan entra un segon missatge humà, els handraises actius anteriors queden `superseded`.

**Passos:**
1. `append_event("c1", "handraise", agent_id="a1")` → seq=1.
2. `record_user_message("c1", ["a1"], message_id="m1")` → seq=2.

**Asserts:**
- Event seq=1 té `status == "superseded"`.
- Event seq=2 té `status == "active"`.
- `supersede_handraises` és idempotent: cridar-la altre cop retorna `rowcount == 0`.

> **Cobert per:** `test_collab_engine.py::test_record_user_message_is_atomic_and_supersedes_old_handraises` + `test_supersede_handraises_is_idempotent`

---

## Grup C — Prioritat i preempció dels missatges de l'usuari (W10)

### C1. Missatge humà durant ronda activa: handraises invalidats

**Descripció:** Si un missatge humà arriba mentre hi ha handraises actius, aquests queden superseded i es reavaluen.

**Passos:**
1. `record_user_message("c1", ["a1", "a2"], message_id="m1")` → seq=1.
2. `transition_receipt("c1", 1, "a1", "will_intervene")`.
3. `transition_receipt("c1", 1, "a2", "will_intervene")`.
4. `record_user_message("c1", ["a1", "a2"], message_id="m2")` → seq=3 (handraise event de pas 2 queda a seq=2).

**Asserts:**
- Els events `agent_state` de seq=2 queden com `active` (no són handraises).
- Els handraises actius queden `superseded`.
- Es creen receipts nous per al seq del nou missatge.

> **Nota:** A la implementació actual, `record_user_message` crida `supersede_handraises` que només marca events de tipus `handraise` com `superseded`. Els events `agent_state` no es toquen. Això és correcte.

### C2. Hand-raise per prioritat: ordre de torns

**Descripció:** Dos agents volen intervenir; el de prioritat més alta parla primer.

**Passos:**
1. Mock de `_quick_completion` que retorna `intervene: true` amb prioritats diferents per agent.
2. Hand-raise: `a1` prioritat 5, `a2` prioritat 2.
3. `volunteers` hauria de ser `["a1", "a2"]` (ordre per prioritat descendent).

**Asserts:**
- `volunteers[0] == "a1"` (prioritat 5).
- `volunteers[1] == "a2"` (prioritat 2).

> **Cobert per:** `test_collab_orchestrator_events.py::test_handraise_orders_by_priority_and_configuration`

### C3. Empat de prioritat: ordre de configuració d'agents

**Descripció:** Dos agents amb mateixa prioritat: l'ordre del `config.agents` desempata.

**Passos:**
1. `config.agents = ["a2", "a1"]` (a2 primer).
2. Hand-raise: `a1` i `a2` amb prioritat 3.

**Asserts:**
- `volunteers[0] == "a2"` (primer a la llista de configuració).

### C4. Mode roundrobin: una passada per tots els agents

**Descripció:** En mode roundrobin, cada agent parla una vegada i la ronda acaba.

**Passos:**
1. `config.mode = "roundrobin"`, agents `["a1", "a2", "a3"]`.
2. `run_round` amb mock de `agent_turn`.

**Asserts:**
- Cada agent rep exactament 1 torn.
- Després de la passada, la ronda acaba (no hi ha hand-raise).
- Els receipts passen `will_intervene` en ordre de configuració.

> **Cobert per:** `test_collab_orchestrator_events.py::test_roundrobin_runs_each_agent_once_and_releases_lease`

---

## Grup D — Lease i recuperació després de reinici (W3/W10)

### D1. Exclusivitat de lease

**Descripció:** Dos workers no poden tenir el lease alhora.

**Passos:**
1. `acquire_lease("c1", "worker-1")` → True.
2. `acquire_lease("c1", "worker-2")` → False.

**Asserts:**
- Worker-1 té el lease; worker-2 no el pot adquirir.

> **Cobert per:** `test_collab_engine.py::test_lease_is_exclusive_and_releasable`

### D2. Lease expirat: un altre worker el pot adquirir

**Descripció:** Si el TTL expira, un altre worker pot agafar el lease.

**Passos:**
1. `acquire_lease("c1", "worker-1", ttl=1)`.
2. `time.sleep(2)` (o mock de `time.time`).
3. `acquire_lease("c1", "worker-2")`.

**Asserts:**
- Worker-2 pot adquirir el lease després de l'expiració.

> **Cobert per:** `test_collab_engine.py::test_expired_lease_can_be_acquired_by_another_worker`

### D3. Lease perdut: sortida neta de la ronda

**Descripció:** Si `_renew_round_lease` detecta que el lease s'ha perdut, la ronda acaba netament amb `lease_lost = True`.

**Passos:**
1. `acquire_lease("c1", "worker-1")`.
2. Simular que un altre worker pren el lease (`acquire_lease("c1", "worker-2")` després d'expirar).
3. `_renew_round_lease` retorna False.

**Asserts:**
- `state["lease_lost"] == True`.
- `state["stop"] == True`.
- `release_lease` es crida amb `stopped=False` (no era una aturada manual).

> **Cobert per:** `test_collab_orchestrator_events.py::test_renew_round_lease_marks_clean_lease_loss`

### D4. Release del lease: un altre worker pot agafar

**Descripció:** Després de `release_lease`, el lease queda lliure.

**Passos:**
1. `acquire_lease("c1", "worker-1")` → True.
2. `release_lease("c1", "worker-1")` → True.
3. `acquire_lease("c1", "worker-2")` → True.

**Asserts:**
- Worker-2 pot adquirir després del release.

> **Cobert per:** `test_collab_engine.py::test_lease_is_exclusive_and_releasable`

### D5. Reconciliació post-reinici (futur — W3)

**Descripció:** Després d'un reinici del backend, `reconcile_channel()` detecta torns orfes (lease expirat, ronda no finalitzada) i els repara.

**Passos:**
1. Adquirir lease i simular crash (no alliberar).
2. Esperar que el TTL expiri.
3. Cridar `reconcile_channel("c1")`.

**Asserts:**
- El lease queda alliberat.
- La sessió passa a `idle`.
- No queden handraises actius sense `superseded`.

> **Nota:** `reconcile_channel()` encara no està implementada. Forma part de W2/W3. Es marca com a test pendent d'implementació.

---

## Grup E — Ordre i idempotència dels esdeveniments

### E1. Seq monotònic sense buits amb 50 insercions concurrents

**Descripció:** 50 insercions concurrents d'events produeixen seqs 1..50 sense buits ni duplicats.

**Passos:**
1. `asyncio.gather(*[append_event("c1", "test") for _ in range(50)])`.

**Asserts:**
- `list_events("c1")` retorna 50 events.
- Els seqs són exactament `{1, 2, 3, ..., 50}`.

> **Cobert per:** `test_collab_engine.py::test_concurrent_events_are_gapless_and_resync_honours_since_limit`

> **Nota:** La implementació actual usa `UPDATE ... RETURNING` (atomicitat garantida a nivell de BD). En SQLite, el write-lock serialitza les insercions. Aquest test verifica que no hi ha buits.

### E2. Idempotència de `transition_receipt`

**Descripció:** Transicionar un receipt al mateix estat dues vegades no genera errors ni events duplicats.

**Passos:**
1. `record_user_message("c1", ["a1"], message_id="m1")`.
2. `transition_receipt("c1", seq, "a1", "evaluating")` → event creat.
3. `transition_receipt("c1", seq, "a1", "evaluating")` → segona crida.

**Asserts:**
- La segona crida retorna un event nou (l'append_event sempre produeix seq nou).
- El receipt es manté en `evaluating` (no canvia res dolent).

> **Cobert per:** `test_collab_engine.py::test_repeated_receipt_transition_keeps_state_and_orders_events`

> ⚠️ **Observació per a l'equip:** la implementació actual de `transition_receipt` sempre crea un `agent_state` event nou, fins i tot si el nou estat és idèntic a l'anterior. No és un bug, però pot generar soroll d'events. Considerar un guard `if old_state == state: return` si cal reduir volum.

### E3. `supersede_handraises` és idempotent

**Descripció:** Cridar `supersede_handraises` 10 vegades amb els mateixos paràmetres deixa el mateix estat.

**Passos:**
1. Crear 5 handraises actius.
2. Cridar `supersede_handraises("c1", 10)` 10 vegades.

**Asserts:**
- Tots 5 queden `superseded` després de la primera crida.
- Les 9 crides següents retornen `rowcount == 0`.

> **Cobert per:** `test_collab_engine.py::test_supersede_handraises_is_idempotent`

### E4. Re-sync: `list_events(since=N)` retorna només events posteriors

**Descripció:** El frontend pot recuperar events perduts demanant des de l'últim seq rebut.

**Passos:**
1. Crear 10 events (seq 1..10).
2. `list_events("c1", since=5)`.

**Asserts:**
- Retorna events amb seq 6..10.
- No retorna seq 1..5.

> **Cobert per:** `test_collab_engine.py::test_concurrent_events_are_gapless_and_resync_honours_since_limit`

### E5. Re-sync amb límit

**Descripció:** `list_events(since=N, limit=M)` retorna com a màxim M events.

**Passos:**
1. Crear 100 events.
2. `list_events("c1", since=0, limit=10)`.

**Asserts:**
- Retorna exactament 10 events.
- Són els primers 10 (ordre per seq ascendent).

> **Cobert per:** `test_collab_engine.py::test_concurrent_events_are_gapless_and_resync_honours_since_limit`

### E6. Deduplicació de missatges humans concurrents

**Descripció:** Dos workers que registren el mateix `message_id` no creen events duplicats; els receipts es combinen.

**Passos:**
1. `asyncio.gather(record(["a1"]), record(["a2"]))` amb el mateix `message_id`.

**Asserts:**
- Ambdós workers obtenen el mateix `seq` (no es crea un segon event).
- `list_receipts` conté tant `a1` com `a2` sobre el mateix `event_seq`.
- El següent `append_event` produeix `seq + 1` (sense buit).

> **Cobert per:** `test_collab_engine.py::test_user_message_is_deduplicated_across_concurrent_workers`

---

## Grup F — Mode fluid sense rondes rígides (W10)

### F1. Mode `rounds` (no-regressió): comportament actual

**Descripció:** El mode per defecte (`handraise`) conserva exactament el comportament d'avui.

**Passos:**
1. `config.mode = "handraise"` (o no definit → default).
2. Missatge humà → hand-raise → torns seqüencials → silenci o guardarail.

**Asserts:**
- El flux és idèntic al comportament previ a W1/W9/W10.
- No hi ha regressió.

### F2. Missatge humà no espera ronda completa en mode `continuous` (futur)

**Descripció:** En mode `continuous`, un missatge humà invalida handraises pendents i reordena la cua sense esperar que la ronda actual acabi.

**Passos:**
1. Mode `continuous` (requereix implementació del scheduler continu).
2. Agent `a1` parlant (torn actiu).
3. Missatge humà nou → handraises invalidats → reavaluació abans del torn següent.

**Asserts:**
- El torn actual d'`a1` no es cancel·la a mig generar (acaba la unitat atòmica).
- El pròxim hand-raise té en compte el missatge nou.

> **Nota:** El mode `continuous` (scheduler complet) encara no està implementat. Aquest test es marca com a pendent fins que W10 Fase 6 estigui llest.

### F3. Endpoint `GET /events?since=&limit=` — controls d'accés

**Descripció:** L'endpoint d'events incrementals respecta els controls d'accés del canal.

**Passos:**
1. Canal privat; usuari sense accés.
2. `GET /api/v1/collab/{channel_id}/events`.

**Asserts:**
- Retorna 403 (Sense accés al canal).

> **Cobert per:** `test_collab_orchestrator_events.py::test_receipts_endpoint_formats_summary_and_propagates_access_denial` (la part final del test verifica el 403)

### F4. Endpoint `GET /receipts/{event_seq}` — format correcte

**Descripció:** L'endpoint retorna receipts amb format estàndard + summary.

**Passos:**
1. Crear receipts per a un missatge humà.
2. `GET /api/v1/collab/{channel_id}/receipts/{event_seq}`.

**Asserts:**
- Response té claus: `event_seq`, `receipts` (llista), `summary` (dict).
- Cada receipt té: `agent_id`, `state`, `message_id`, `updated_at`.

> **Cobert per:** `test_collab_orchestrator_events.py::test_receipts_endpoint_formats_summary_and_propagates_access_denial` (la part inicial del test)

---

## Resum de cobertura

| Grup | Tests | Coberts existents | Pendents |
|---|---|---|---|
| A — Estats d'agent | 4 | 1 (A4 via self-reply test) | 3 |
| B — Receipts per missatge | 5 | 4 (B2, B3, B4, B5) | 1 |
| C — Prioritat i preempció | 4 | 2 (C2, C4) | 2 |
| D — Lease i recuperació | 5 | 4 (D1, D2, D3, D4) | 1 |
| E — Ordre i idempotència | 6 | 5 (E1, E2, E3, E4, E5, E6) | 0 |
| F — Mode fluid | 4 | 2 (F3, F4) | 2 |
| **Total** | **28** | **18** | **9** |

### Tests marcats com a "pendent d'implementació"
- **D5** — `reconcile_channel()` (W2/W3, no implementat encara).
- **F2** — Mode `continuous` / scheduler complet (W10 Fase 6, no implementat).
- Aquests 2 tests es poden escriure però es marquen com `@pytest.mark.skip(reason="W3/W10 no implementat")`.

### Tests pendents implementables ara (sense dependències externes)
- **A1** — Cicle complet d'estats (inclou `speaking` i torn a `idle`).
- **A2** — Agent que fa `pass`.
- **A3** — Agent caigut i recuperació.
- **B1** — Un receipt per agent (cas simple).
- **C1** — Preempció: missatge humà durant ronda.
- **C3** — Empat de prioritat: ordre de configuració.
- **F1** — No-regressió del mode `handraise`.

### Prioritat d'implementació recomanada
1. **Grup B** (receipts) — els més crítics per W9, infraestructura de mock ja existeix.
2. **Grup E** (idempotència) — verify que el sistema és robust.
3. **Grup A** (estats) — verify transicions visibles.
4. **Grup D** (lease) — la majoria ja coberts.
5. **Grup C** (prioritat) — verify ordre de torns.
6. **Grup F** (mode fluid) — pendent de implementació del scheduler continu.

---

## Nota sobre el mock de `time.time`

Per als tests de lease i TTL (D2, D3), cal mockar `time.time()` per simular el pas del temps sense esperar realment:

```python
@pytest.fixture
def mock_time(monkeypatch):
    current = [1700000000]
    def fake_time():
        return current[0]
    def advance(seconds):
        current[0] += seconds
    monkeypatch.setattr("open_webui.collab.engine.time.time", fake_time)
    return advance
```

Per als tests de `turn_timeout` (A3, C2), cal patchejar `config.guardrail("turn_timeout")` a un valor curt (p.ex. 1s).
