# Specs de Tests — W2/W3: Cancel·lació segura i ronda recuperable

> Aquest document defineix els tests que han de passar la implementació de
> W2 (cancel·lació i timeout reals) i W3 (una sola ronda, recuperable).
> Es complementa amb `docs/disseny-w2-w3-cancelacio-recuperacio.md`.

---

## Preàmbul: entorn de test

Tots els tests operen sobre `engine.py` amb SQLite en memòria (igual que
`test_collab_engine.py`), aïllant la capa de persistència.

Els tests d'integració amb l'orquestrador fan servir mocks per als models
(no criden cap API real) i verifiquen el flux de control.

---

## W2: Cancel·lació segura i timeout hard

### W2-T1: cancel_turn atura el torn en curs

**Objectiu:** verificar que `cancel_turn(turn_id)` cancel·la el
`asyncio.Task` actiu del torn i que el missatge placeholder queda marcat com
cancel·lat (no `done=True` amb contingut parcial).

**Passos:**
1. Iniciar `run_round` amb un agent mock que triga 10s.
2. Cridar `cancel_turn(channel_id, turn_id)` abans que acabi.
3. Verificar que l'`asyncio.Task` del torn ha estat cancel·lat (`CancelledError`).
4. Verificar que el missatge placeholder té `meta.done=True` i contingut amb
   marcador de cancel·lació (p.ex. `⚠️ Torn cancel·lat per l'usuari`).

**Criteri:** el torn s'atura en menys de 2s després de la crida.

---

### W2-T2: cancel_turn no afecta altres torns

**Objectiu:** verificar que cancel·lar un torn no interfereix amb rondes
futures o altres canals.

**Passos:**
1. Iniciar ronda al canal A amb agent lent.
2. Cancel·lar el torn al canal A.
3. Iniciar ronda al canal B simultàniament.
4. Verificar que el canal B funciona normalment.

**Criteri:** el canal B completa la seva ronda sense incidències.

---

### W2-T3: timeout hard no desactivable (600s)

**Objectiu:** verificar que el timeout hard de 600s atura qualsevol torn,
independentment del `turn_timeout` configurat al canal.

**Passos:**
1. Configurar `turn_timeout=0` (sense timeout soft).
2. Mockar un agent que triga indefinidament (mai retorna).
3. Amb el timeout hard reduït a 2s per al test (injectar `_HARD_TIMEOUT=2`).
4. Verificar que el torn es talla als 2s amb missatge d'avís.

**Criteri:** el torn s'atura exactament al timeout hard, no abans ni després.

---

### W2-T4: tool_lock protegeix operacions amb efectes

**Objectiu:** verificar que les operacions amb efectes (escriptura de
fitxers, propose_finish, update_task) queden protegides per `tool_lock` i
que una cancel·lació no les interromp a mig camí.

**Passos:**
1. Mockar un agent que inicia `write_project_file` i triga 3s.
2. Cancel·lar el torn a 1s.
3. Verificar que `write_project_file` completa la seva escriptura.
4. Verificar que no hi ha fitxer parcial o corrupte.

**Criteri:** l'operació protegida completa atòmicament malgrat la
cancel·lació; el torn s'atura després, no durant.

---

### W2-T5: endpoint POST /collab/turn/cancel

**Objectiu:** verificar l'endpoint REST de cancel·lació.

**Passos:**
1. Iniciar ronda al canal.
2. Cridar `POST /api/v1/collab/{channel_id}/turn/cancel`.
3. Verificar resposta `{"cancelled": true}`.
4. Cridar de nou quan no hi ha torn actiu.
5. Verificar resposta `{"cancelled": false}`.

**Criteri:** l'endpoint retorna l'estat correcte i dispara la cancel·lació.

---

## W3: Una sola ronda, recuperable

### W3-T1: lease persistent impedeix doble ronda

**Objectiu:** verificar que si un worker adquireix el lease, un segon
worker no pot iniciar `run_round` al mateix canal.

**Passos:**
1. Worker-1 adquireix lease al canal C.
2. Worker-2 intenta `run_round` al canal C.
3. Verificar que `run_round` retorna immediatament sense executar cap torn.
4. Worker-1 allibera el lease.
5. Worker-2 intenta `run_round` de nou.
6. Verificar que ara s'executa normalment.

**Criteri:** no hi ha mai dues rondes concurrents al mateix canal.

---

### W3-T2: lease expirat permet recuperació

**Objectiu:** verificar que un lease expirat (worker caigut sense
alliberar) permet a un altre worker prendre el reléu.

**Passos:**
1. Worker-1 adquireix lease amb TTL=2s.
2. Worker-1 "mor" (no renova, no allibera).
3. Esperar 3s (lease expirat).
4. Worker-2 adquireix lease.
5. Verificar que `acquire_lease` retorna `True`.

**Criteri:** el lease expirat es pot capturar sense necessitat d'alliberament
explícit.

---

### W3-T3: pèrdua del lease durant la ronda → sortida neta

**Objectiu:** verificar que si `_renew_round_lease` detecta pèrdua del
lease (un altre worker ha pres el reléu), la ronda actual s'atura netament
sense marcar falsament una aturada manual.

**Passos:**
1. Worker-1 inicia ronda amb agent lent.
2. Simular que el lease és sobreescrit per Worker-2 (forçar
   `lease_owner != worker-1` a la DB).
3. Verificar que `_renew_round_lease` detecta la pèrdua.
4. Verificar que `state["lease_lost"] = True` i `state["stop"] = True`.
5. Verificar que `release_lease` al `finally` no marca `stopped=True`.

**Criteri:** la ronda s'atura sense avís d'aturada manual; no hi ha
doble processament.

---

### W3-T4: reconcile_channel recupera torns orfes post-reinici

**Objectiu:** verificar que després d'un reinici del servidor, els canals
amb sessions `active` i lease expirat es reconcilien.

**Passos:**
1. Crear sessió `active` amb lease_owner antic i `lease_expires_at` passat.
2. Cridar `reconcile_channel(channel_id)`.
3. Verificar que la sessió es marca `idle` i el lease s'allibera.
4. Verificar que els events `active` sense receipts es marquen per
   reprocessament.

**Criteri:** cap canal queda en estat irrecuperable després d'un reinici.

---

### W3-T5: mode continuous — missatge humà no espera ronda completa

**Objectiu:** verificar que en mode `continuous`, un missatge humà nou
mentre hi ha una ronda en curs s'integra al context del proper hand-raising,
sense bloquejar ni iniciar una segona ronda.

**Passos:**
1. Iniciar ronda al canal en mode `continuous`.
2. Mentre la ronda està en marxa, rebre un missatge humà.
3. Verificar que `handle_collab_message` retorna `True` sense iniciar una
   segona ronda (`channel.id in _active_rounds` → early return).
4. Verificar que el missatge queda persistit (event + receipts) perquè la
   ronda en curs el vegi al proper cicle de transcripció.

**Criteri:** el missatge humà s'integra sense duplicar rondes ni perdre's.

---

### W3-T6: mode roundrobin — una passada i s'acaba

**Objectiu:** verificar que en mode `roundrobin`, la ronda fa exactament
una passada per tots els agents i s'atura.

**Passos:**
1. Configurar 3 agents en mode `roundrobin`.
2. Iniciar ronda.
3. Verificar que cada agent parla exactament un cop.
4. Verificar que la ronda s'atura després del tercer agent.

**Criteri:** no hi ha segona passada ni bucle infinit.

---

## Cobertura esperada

| Test | Bloc | Propietat verificada |
|---|---|---|
| W2-T1 | W2 | Cancel·lació atura el torn |
| W2-T2 | W2 | Aïllament entre canals |
| W2-T3 | W2 | Timeout hard no desactivable |
| W2-T4 | W2 | tool_lock protegeix operacions crítiques |
| W2-T5 | W2 | Endpoint REST de cancel·lació |
| W3-T1 | W3 | Lease impedeix doble ronda |
| W3-T2 | W3 | Lease expirat permet recuperació |
| W3-T3 | W3 | Pèrdua de lease → sortida neta |
| W3-T4 | W3 | Reconciliació post-reinici |
| W3-T5 | W3 | Mode continuous no duplica rondes |
| W3-T6 | W3 | Mode roundrobin = una passada |

**Total: 11 tests** (5 per W2, 6 per W3).

---

## Nota sobre mocks

Els tests d'integració necessiten:

1. **Mock de `generate_chat_completion`**: retorna una resposta amb un
   retard configurable per simular agents lents.
2. **Mock de `request.app.state.CHAT_COMPLETION_HANDLER`**: igual que
   l'anterior, per als torns streaming.
3. **Mock de `Messages.get_message_by_id`**: retorna missatges amb
   `meta.done` controlable.
4. **Patch de `_HARD_TIMEOUT`**: per reduir el timeout hard de 600s a 2s
   en els tests.

Aquestes infraestructures de mock es poden reaprofitar per als tests
d'integració de W1/W9/W10 que faltin.
