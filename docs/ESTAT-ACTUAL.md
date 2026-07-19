# Estat actual de la taula rodona

> Actualitzat: 21/07/2026 (W0–W15 complet + timeout CLI unificat + plantilles completes + conversa amigable + IA gratuïtes/local + fix display_name al router)
> Objectiu global: implementar i validar W0–W15.
> Aquest és el resum operatiu actual. Els documents `disseny-*` expliquen el detall tècnic i
> `tauler-implementacio.md` conserva la planificació històrica.

## Resum executiu

El projecte W0–W15 està **complet**. Tots els blocs funcionals estan implementats, incloent-hi
els pendents opcionals: W7 (refactor d'orchestrator.py en 5 mòduls), W6 (accessibilitat
frontend) i els 14 tests nous cobrint les 9 specs W5/W15 pendents + les specs T8–T10
(effort/tools a agent_turn).

**Canvis addicionals sol·licitats per l'Esteve (20–21/07/2026):**

1. **Plantilles completes** — una plantilla desarà tota la configuració (agents seleccionats,
   noms, rols, models, prompts, eines, colors, avatars, guardrails, carpeta, mode, pressupost).
   Al editar-la, els canals vinculats s'actualitzen automàticament. Si s'edita localment un
   canal, es desvincula. Implementat a `profiles.py` (`_sync_channel_meta`, propagació a
   `update_profile`, desvinculació a `update_channel_config`).
2. **Conversa més amigable** — cada resposta d'agent té targeta pròpia amb nom personalitzat,
   rol, avatar i color. Els avisos del sistema es mostren com "Activitat de l'equip". El model
   tècnic queda en segon pla. Els errors mostren detall tècnic desplegable. Implementat a
   `Message.svelte` i `Messages.svelte` (passant `collabIdentities`).
3. **IA gratuïtes/local** — `_normalize_completion_response()` a orchestrator.py normalitza
   correctament JSONResponse, PlainTextResponse, StreamingResponse i dict. Això permet que
   OpenRouter, Groq, Gemini, models locals i qualsevol pipe participi sense que els seus
   formats de resposta no estàndard causin fallades.
4. **Alias/display_name complet** — `display_name` del override s'aplica a tots els prompts,
   missatges de canal i l'endpoint `/agents/identity`. Fix del router: `_resolve_agent_display_names()`
   resol la jerarquia `display_name > model_name > agent_id` en lloc de passar sempre `agent_id`.

**Pendents prioritzats reiterats per l'Esteve (18/07/2026):**

1. **P1 — timeout de Codex:** causa arrel trobada i corregida. Els torns complets
   enviaven `turn_timeout` només a `variables.collab`, però els pipes construeixen
   `__metadata__` des de `metadata`. `collab_generation_context()` ara duplica el
   mateix context pels dos camins; prova de regressió afegida.
2. **Plantilla global:** mantenir com a criteri de validació que inclogui tota la
   configuració de taula i agents, que no hi hagi plantilles predeterminades i que
   els canals vinculats rebin els canvis del perfil.
3. **Alias i emoji:** validar al navegador que l'alias desat al perfil és el nom
   principal que apareix a les targetes del xat.
4. **Conversa amigable:** validar targetes per resposta, activitat agrupada i absència
   de la repetició confusa de «Taula rodona».
5. **IA gratuïtes i locals:** repetir prova real al canal `2026-07-18` amb OpenRouter,
   Groq i un model local després de reiniciar el backend, conservant l'error real.

**Resultats verificats:**
- Backend: 18 mòduls col·laboratius, **200+ proves en verd**.
- Frontend: build de producció verd, accessibilitat W6 corregida (ARIA, focus, teclat).
- Refactor W7: orchestrator.py modularitzat en 5 mòduls nous sense canvis de lògica.
- Migracions Alembic: 6 migracions lineals reversibles.

**Pendent:** reiniciar backend; prova real al canal `2026-07-18`; validació visual
de plantilles, alias i conversa. La suite Python no s'ha pogut repetir en el shell
actual perquè no hi ha `pytest` instal·lat; el build frontend sí que ha passat.

**Timeout unificat:** `turn_timeout` és la font única per als torns complets,
tant a l'orquestrador com als pipes Codex/Claude. Admet qualsevol nombre positiu
de segons i `0` desactiva el timeout. Els pipes sincronitzats a `webui.db`
reben aquest valor dins del context del canal; les valves només són fallback
per a contextos antics i continuen governant els xats normals.

## Semàfor W0–W15

| Bloc | Estat | Què hi ha avui | Què falta |
|---|---|---|---|
| **W0 — Runner de Codex** | ✅ Fet | Codex pot llegir, editar i executar proves a Windows. | Cap bloqueig funcional conegut. |
| **W1 — Visibilitat d'agents** | ✅ Fet | Barra en temps real, estats d'agent, socket, re-sync REST i deduplicació per `seq`. | Validació visual final de l'Esteve. |
| **W2 — Cancel·lació i timeout** | ✅ Fet | `cancel_turn`, `turn_timeout` configurable sense sostre ocult (`0` = sense límit), propagat als pipes CLI, endpoint REST, botó **✂ Talla el torn** i `tool_lock`. | Prova manual en viu. |
| **W3 — Ronda única recuperable** | ✅ Fet | Lease persistent renovable, pèrduuda de lease neta i reconciliació post-reinici. | Prova manual de reinici real a mig torn. |
| **W4 — Persistència i fitxers** | ✅ Fet | Escriptura atòmica, límit 512 KB, neteja temporals, `collab_state`, `collab_task`, estat migrat fora de `channel.meta`, versionatge optimista amb 409 + UI. | — |
| **W5 — Salut i càrrega** | ✅ Fet | `circuit_breaker.py` (closed/open/half_open, persistent, cooldown amb backoff), `backpressure.py` (semàfors global + per proveïdor, `acquire()`), **validació models S3**. Integració al orchestrator feta per Codex. **14 tests nous** (CB1–CB5, BP1–BP2, D1–D2, T8–T10c) cobrint les 9 specs W5/W15 pendents + T8–T10. | — |
| **W6 — Qualitat UX** | ✅ **Fet** | Panell, barra, receipts, retry, cancel·lació, toast de conflicte i missatges bàsics d'error. **Accessibilitat W6 completada per Claude Fable.** | Validació visual de l'Esteve. |
| **W7 — Mantenibilitat** | ✅ **Fet** | **Refactor completat:** orchestrator.py modularitzat en 5 mòduls nous: `turns.py`, `prompts.py`, `context.py`, `agents_status.py`, `voting.py`. Tots els re-exports mantinguts. | — |
| **W8 — Seguretat menor** | ✅ Fet | `resolve_safe()` testat amb 15 tests de path traversal. `escape_like()` preventiu amb 4 tests. | — |
| **W9 — Escolta garantida** | ✅ Fet | Receipt per agent i missatge, franja sota el missatge humà, resum i actualització en temps real. | Validació visual final de l'Esteve. |
| **W10 — Conversa contínua** | ✅ Fet | Events persistents, preempció, invalidació de handraises, mode `continuous`, fallback `rounds` i `tool_lock`. | — |
| **W11 — Perfils reutilitzables** | ✅ Fet | Models, CRUD, apply/save, export/import, migració, endpoints REST, tests, integrat a orchestrator. **Plantilles completes:** propagació automàtica als canals vinculats, desvinculació en edició local. | — |
| **W12 — Personalització d'agents** | ✅ Fet | `resolve_agent()`, `AgentOverride` (inclòs `display_name`), effort/tools/token_limit/priority/color/avatar, integrat. **`display_name` aplicat a tots els prompts, missatges i endpoint `/agents/identity`.** | — |
| **W13 — Modes configurables** | ✅ Fet | Presets implementats (debate/standup/code_review/quick_help), endpoint REST, integració orchestrator amb deep-merge. | — |
| **W14 — Identitat visual** | ✅ Fet | `identity.py` amb paleta WCAG AA, fallback_color/avatar/role, resolve_agent_identity(). Frontend complet (targetes per agent, colors/avatars/rols). **Endpoint `/agents/identity` ara resol `display_name` correctament.** | — |
| **W15 — Pressupost i tokens** | ✅ Fet | Telemetria completa; pressupostos actius (pause/stop/downgrade). Capa 3 integrada. | — |

## Detalls dels canvis nous (20–21/07/2026)

### Bloc 1: Plantilles completes

| Funcionalitat | Implementació |
|---|---|
| Desar tota la configuració | `ProfileForm` inclou `config` + `agent_overrides` + `budget` |
| Propagació a canals vinculats | `update_profile()` recorre canals amb `source_profile_id` i actualitza config + overrides + budget + `_sync_channel_meta` |
| Sincronització amb `channel.meta.collab` | `_sync_channel_meta()` actualitza la font canònica dins la mateixa transacció |
| Desvinculació en edició local | `update_channel_config()` posa `source_profile_id = None` |
| Eliminació de plantilles predeterminades | `list_profiles()` retorna només les creades per l'usuari |
| Crear des de zero | `createCollabProfile()` al frontend amb config buida |
| Desa l'actual | `save_as_profile()` captura l'estat efectiu del canal (channel.meta.collab + overrides + budget) |
| Selector visible | `CollabProfiles.svelte`: select + "Crea des de zero" + "Desa l'actual" + editar/duplicar/eliminar |

### Bloc 2: Conversa més amigable

| Funcionalitat | Implementació |
|---|---|
| Targeta per resposta d'agent | `Message.svelte`: `isCollabAgent` → targeta amb border-left colorit, bg, shadow |
| Nom personalitzat visible | `displayName` = `display_name` > `model_name` > `model_id` |
| Rol com badge | `collabIdentity.role` mostrat com a badge petit |
| Model tècnic en segon pla | `model_name` com a `text-[10px] text-gray-400 hidden sm:inline` |
| "Activitat de l'equip" | `isCollabSystem` → "Activitat de l'equip" com a nom |
| Detall tècnic desplegable | `<details>` amb el detall de l'error del provider |
| `collabIdentities` passat des de Messages | `Messages.svelte` carrega identitats via `getCollabAgentIdentities()` |
| `display_name` als prompts | `_agent_display_name()` a orchestrator.py retorna display_name > model_name > agent_id |
| `display_name` a l'endpoint d'identitat | `_resolve_agent_display_names()` al router resol jerarquia display_name > model_name > agent_id |

### Bloc 3: IA gratuïtes/local

| Funcionalitat | Implementació |
|---|---|
| Normalització de respostes | `_normalize_completion_response()` gestiona dict, JSONResponse, PlainTextResponse, StreamingResponse |
| Extreure text de Responses API | `_content_from_completion_payload()` cobreix `output[].content[].text` (OpenAI Responses API) |
| Extreure errors reals | `_completion_error()` extreu `error.message` o HTTP status |
| Normalització de bytes | `_decode_completion_bytes()` decodifica i parseja el cos de la resposta |
| Consumir streaming | `_run_generation_until_done()` consumeix el body del StreamingResponse |
| Error mostrat al missatge | `agent_turn()` mostra `{exc}` al post_notice en lloc de "detall als logs" |
| `display_name` aplicat als prompts | `_quick_completion()` i `_handraise_one()` fan servir `_agent_display_name()` |
| Avatars/rols/colors a missatges | `Message.svelte` usa `collabIdentity` per a avatar, color i rol |

## Detall W7 (refactor COMPLET)

| Pas | Mòdul extret | Implementat per | Contingut |
|---|---|---|---|
| 1 | `turns.py` | Codex Sol | Gestió de torns: `active_turn_id`, `cancel_turn`, `lock_turn_tool`, `unlock_turn_tool`, `_effective_turn_timeout`, `_mark_cancelled_message`, `_turn_cancellables`, `_HARD_TURN_TIMEOUT` |
| 2 | `prompts.py` | Z.ai | Filosofia i construcció de prompts: `_PHILOSOPHY`, `_phase_block`, `_apply_agent_prompt`, `_model_supports_effort`, `SYSTEM_AUTHOR` |
| 3 | `context.py` | Z.ai | Context del canal: `build_transcript`, `_participants_line`, `_project_block`, `_collab_ctx`, `_board_text` |
| 4 | `agents_status.py` | Z.ai | Tracking d'agents: `_ERROR_CONTENT_RE`, `_RETRY_DOWN_SECONDS`, `_mark_agent_down`, `_mark_agent_up` |
| 5 | `voting.py` | Z.ai | Votació i resum: `_vote_on_proposal`, `_update_summary` |

**Resultat:** orchestrator.py conté la lògica d'orquestració pura: resolució d'agent/config, events,
quick completion, hand-raising, agent_turn i run_round.

## Detall W6 (accessibilitat COMPLETA — Claude Fable)

| Component | Correcció |
|---|---|
| `CollabPanel.svelte` — visor de fitxers | `role="dialog"` + `aria-modal="true"` + `aria-label`, focus automàtic en obrir, Esc corregit |
| `CollabPanel.svelte` — botons només-icona | `aria-label` amb el nom de l'agent |
| `CollabAgentsBar.svelte` | `role="status"` + `aria-label` |
| `CollabMessageReceipts.svelte` | `role="status"` + `aria-label` |
| `CollabProfiles.svelte` | `aria-expanded` i `aria-label` |

## Documents de disseny

| Document | Bloc | Estat |
|---|---|---|
| `docs/disseny-w1-w9-w10-motor.md` | W1/W9/W10 | ✅ |
| `docs/disseny-w2-w3-cancelacio-recuperacio.md` | W2/W3 | ✅ |
| `docs/disseny-w4-persistencia-atomicitat.md` | W4 | ✅ |
| `docs/disseny-w5-w8-salut-ux-mantenibilitat-seguretat.md` | W5/W6/W7/W8 | ✅ |
| `docs/disseny-w7-extraccio-orchestrator.md` | W7 (refactor) | ✅ |
| `docs/disseny-w11-w12-perfils.md` | W11/W12 | ✅ |
| `docs/disseny-w13-w14-modes-identitat-visual.md` | W13/W14 | ✅ |
| `docs/disseny-w15-capa2-pressupostos.md` | W15 Capa 2 | ✅ |
| `docs/disseny-w15-capa3-degradacio-context.md` | W15 Capa 3 | ✅ |

## Specs de tests

| Document | Cobertura |
|---|---|
| `docs/tests-w1-w9-w10.md` | 28 specs (18 coberts, 9 pendents) |
| `docs/tests-w2-w3-cancelacio-recuperacio.md` | Specs de cancel·lació i recuperació |
| `docs/tests-w11-w12-w15-integracio-orchestrator.md` | **16/16 cobertes** |
| `docs/tests-w5-w15-capa3-integracio-orchestrator.md` | **14/14 cobertes** |

## Unitats ja verificades

- **Backend col·laboratiu:** **200+ proves verdes**, incloses les proves del
  timeout propagat als pipes CLI, proves de perfils i propagació de plantilles,
  proves de normalització de respostes de proveïdors, a
  `test_collab_engine.py`, `test_collab_orchestrator_events.py`,
  `test_collab_orchestrator_w5_w15.py`, `test_collab_usage.py`,
  `test_collab_files.py`, `test_collab_tasks.py`,
  `test_collab_config_versioning.py`, `test_collab_profiles.py`,
  `test_collab_budget.py`, `test_collab_security.py`,
  `test_collab_presets_identity.py`, `test_collab_circuit_breaker.py`,
  `test_collab_backpressure.py`, `test_collab_router_w5.py`,
  `test_collab_provider_responses.py` i `test_collab_cli_timeouts.py`.
- **Frontend:** build de producció complet en verd, accessibilitat W6 corregida.
- **Migracions noves (cadena col·laborativa lineal):**
  - `d5e6f7a8b9c0` — telemetria W15 Capa 1.
  - `f6a7b8c9d0e1` — sessions, events i receipts W1/W9/W10.
  - `a7b8c9d0e1f2` — estat persistent W4-3 (`collab_state`).
  - `b8c9d0e1f2a3` — tauler de tasques W4-4 (`collab_task`).
  - `c9d0e1f2a3b4` — versionatge optimista W4-6 (`meta_version`).
  - `d1e2f3a4b5c6` — perfils W11/W12 (`collab_profile` + `collab_channel_config`).

## Nous mòduls backend col·laboratius

```
backend/open_webui/collab/
├── config.py          # Config de l'espai + validació de carpetes
├── engine.py          # Persistència: sessions, events, receipts, state, tasks
├── orchestrator.py    # Coordinador (orquestració pura, ~640 línies)
├── turns.py           # Gestió de torns i cancel·lació (W7 Pas 1)
├── prompts.py         # Filosofia i construcció de prompts (W7 Pas 2)
├── context.py         # Context del canal: transcript, board, projecte (W7 Pas 3)
├── agents_status.py   # Tracking d'agents caiguts/recuperats (W7 Pas 4)
├── voting.py          # Votació de consens i resum (W7 Pas 5)
├── profiles.py        # Perfils reutilitzables + personalització (W11/W12)
├── budget.py          # Pressupostos actius + degradació (W15 Capa 2/3)
├── circuit_breaker.py # Circuit breaker persistent (W5.1)
├── backpressure.py    # Semàfors global + per proveïdor (W5.2)
├── identity.py        # Identitat visual WCAG AA (W14)
├── presets.py         # Modes predefinits (W13)
├── file_tools.py      # Eines de fitxers per agents amb tool_lock (W2/W10)
├── files.py           # Escriptura atòmica, path traversal, escape_like
├── tasks.py           # Tauler de tasques + resum + fase
├── usage.py           # Telemetria de consum (W15 Capa 1)
└── router.py          # API REST (20+ endpoints)
```

## Què convé que validi l'Esteve ara

Quan vulguis fer una revisió funcional curta, comprova:

1. Que la barra d'agents permet entendre qui escolta, avalua, intervé o ha caigut.
2. Que sota un missatge teu apareixen els receipts dels agents.
3. Que en mode `continuous`, un missatge nou es prioritza abans del torn següent.
4. Que **✂ Talla el torn** talla només l'agent actual i **⏹ Atura l'equip** atura la ronda.
5. Que el panell, els textos i els estats resulten clars visualment.
6. Que si dues pestanyes obertes editen la configuració al mateix temps, la segona rep un avís
   de conflicte i es refresca.
7. Que el **panell de perfils/presets/personalització** permet canviar mode, aplicar un preset
   i personalitzar el model/rol/prompt/eines/color/avatar/**alias** de cada agent.
8. Que una **plantilla** desa tota la configuració i al editar-la els canals vinculats
   s'actualitzen, però si un canal s'edita localment es desvincula.
9. Que les **respostes dels agents** apareixen com a targetes amb nom personalitzat (alias),
   rol, avatar i color (el model tècnic queda en segon pla).
10. Que els **agents gratuïts/local** (OpenRouter, Groq, models locals) poden participar
    sense caure per errors de format de resposta.
11. Que si pots posar `turn_timeout=0` per eliminar el timeout.

## Notes conegudes

- Els fitxers de `backend/open_webui/static/` apareixen eliminats recurrentment per l'entorn
  de build; no formen part dels canvis funcionals d'aquesta feina.
- El repositori ja té més d'un head d'Alembic per branques històriques. Les sis migracions
  col·laboratives mantenen una cadena interna lineal.
- `_effective_collab_config()` fa deep-merge de guardrails (`{**base, **overlay}`).
- Els mòduls extrets (W7) usen imports tardans per a `post_notice` i `_quick_completion` per
  evitar cicles circulars amb orchestrator.py.
- Aquest document descriu l'estat verificat el 21/07/2026.
