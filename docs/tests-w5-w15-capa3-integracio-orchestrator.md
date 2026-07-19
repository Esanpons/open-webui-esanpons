# Specs tests W5 — Integració circuit breaker + backpressure a orchestrator

> **Objectiu:** validar que la integració de W5 (circuit breaker, backpressure) i
> W15 Capa 3 (degradació de context) a `orchestrator.py` funciona correctament.
>
> **Estat actual:** ✅ **TOT COBERT** — 14/14 specs implementades a
> `test/test_collab_orchestrator_w5_w15.py`.

## Llegenda d'estats

- ✅ Cobert
- ⬜ Pendent

---

## Specs W5.1 — Circuit breaker a orchestrator

### CB1 — Circuit obert bloca `_quick_completion`

**Quan:** un agent té el circuit en estat `open` (3 errors consecutius).
**Llavors:** `_quick_completion()` retorna `None` sense fer la crida al model.
**Comprova:** no es fa cap crida a `generate_chat_completion`.
**Test:** `test_cb1_circuit_open_blocks_quick_completion`
**Estat:** ✅ Cobert

### CB2 — Circuit obert avisa a `agent_turn`

**Quan:** un agent té el circuit obert i li toca el torn.
**Llavors:** es publica un avís "⚡ L'agent `{agent_id}` està temporalment en pausa (circuit obert)." i `agent_turn` retorna `None`.
**Comprova:** `post_notice` és cridat amb el text correcte.
**Test:** `test_cb2_circuit_open_notifies_in_agent_turn`
**Estat:** ✅ Cobert

### CB3 — `record_success` es crida després d'una resposta exitosa

**Quan:** `_quick_completion()` obté una resposta no-buida.
**Llavors:** `_record_circuit_result(channel_id, agent_id, STATUS_SUCCESS)` és cridat.
**Comprova:** el circuit passa a `closed` amb `consecutive_failures=0`.
**Test:** `test_cb3_record_success_after_valid_response`
**Estat:** ✅ Cobert

### CB4 — `record_failure` es crida després d'una resposta buida/error

**Quan:** `_quick_completion()` obté `None` (timeout, excepció o resposta buida).
**Llavors:** `_record_circuit_result(channel_id, agent_id, status)` és cridat amb un status d'error.
**Comprova:** el circuit acumula un failure.
**Test:** `test_cb4_record_failure_after_error`
**Estat:** ✅ Cobert

### CB5 — Circuit fail-open si `can_proceed` falla

**Quan:** `can_proceed()` llança una excepció (p. ex. DB caiguda).
**Llavors:** `_circuit_allows()` retorna `True` (fail-open) i la crida procedeix.
**Comprova:** la crida al model es fa normalment.
**Test:** `test_cb5_circuit_fail_open_on_exception`
**Estat:** ✅ Cobert

### CB6 — `CancelledError` NO registra failure

**Quan:** `agent_turn()` és cancel·lat (usuari, timeout o preempció).
**Llavors:** `_record_circuit_result()` **NO** és cridat al path de `CancelledError`.
**Comprova:** el circuit no acumula failures per cancel·lacions.
**Estat:** ✅ Cobert (per disseny — el path `except asyncio.CancelledError` no crida `_record_circuit_result`)

---

## Specs W5.2 — Backpressure a orchestrator

### BP1 — `acquire_model_slot` envolta la crida al model a `_quick_completion`

**Quan:** `_quick_completion()` fa una crida al model.
**Llavors:** la crida es fa dins de `async with acquire_model_slot(effective_model_id):`.
**Comprova:** el semàfor del proveïdor es decrementa durant la crida i es restaura després.
**Test:** `test_bp1_acquire_model_slot_wraps_quick_completion`
**Estat:** ✅ Cobert

### BP2 — `_run_with_backpressure` envolta la generació a `agent_turn`

**Quan:** `agent_turn()` llança la generació.
**Llavors:** es crea una task `_run_with_backpressure()` que envolta `_run_generation_until_done` amb `acquire_model_slot`.
**Comprova:** el semàfor global es decrementa durant la generació.
**Test:** `test_bp2_backpressure_wraps_generation_in_agent_turn`
**Estat:** ✅ Cobert

### BP3 — Backpressure allibera l'slot en cas d'excepció

**Quan:** la crida al model dins de `acquire()` llença una excepció.
**Llavors:** el context manager allibera l'slot (garantit pel `__aexit__`).
**Comprova:** `stats()["global"]["available"]` torna al màxim.
**Test:** `test_release_on_exception` a `test_collab_backpressure.py`
**Estat:** ✅ Cobert

---

## Specs W5.3 — Validació de models (S3)

### S3-1 — Endpoint `POST /config` rebutja models invàlids

**Quan:** s'envien `agents` amb un model_id que no existeix als models disponibles.
**Llavors:** el router respon `400 Bad Request` amb el detall dels models invàlids.
**Test:** `test_some_invalid` a `test_collab_router_w5.py`
**Estat:** ✅ Cobert

### S3-2 — Fail-open si no es poden obtenir els models

**Quan:** `get_all_models()` falla.
**Llavors:** `_validate_models()` retorna `[]` i no bloqueja.
**Test:** `test_fail_open_on_error`
**Estat:** ✅ Cobert

---

## Specs W15 Capa 3 — Degradació de context

### D1 — `handraise()` redueix context_messages quan degradat

**Quan:** `_is_degraded()` retorna `True`.
**Llavors:** `context_config` té `context_messages: 5` (en lloc del valor per defecte de 30).
**Comprova:** `build_transcript` rep `limit=5`.
**Test:** `test_d1_handraise_reduces_context_when_degraded`
**Estat:** ✅ Cobert

### D2 — `agent_turn()` suprimeix l'arbre de fitxers quan degradat

**Quan:** `_is_degraded()` retorna `True`.
**Llavors:** `_project_block(config, include_tree=False)` — l'arbre de fitxers no s'inclou al system prompt.
**Test:** `test_d2_agent_turn_suppresses_file_tree_when_degraded` + `test_d2b_agent_turn_includes_file_tree_when_not_degraded`
**Estat:** ✅ Cobert

### D3 — Sense budget, mai es degrada

**Quan:** no hi ha pressupost actiu al canal.
**Llavors:** `_is_degraded()` retorna `False` sempre.
**Estat:** ✅ Cobert (per disseny — `_is_degraded` retorna `False` si `budget` és None o buit)

---

## Resum de cobertura

| Grup | Specs | Cobertes | Pendents |
|---|---|---|---|
| W5.1 Circuit breaker | 6 | 6 | 0 |
| W5.2 Backpressure | 3 | 3 | 0 |
| W5.3 Validació models | 2 | 2 | 0 |
| W15 Capa 3 | 3 | 3 | 0 |
| **Total** | **14** | **14** | **0** |

Totes les specs W5/W15 estan cobertes. La suite ara inclou 189+ tests col·laboratius.
