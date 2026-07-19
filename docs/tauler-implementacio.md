# Tauler d'implementació — Pla W0–W15 (`docs/auditoria-collab.md`)

> **Nota:** aquest tauler conserva la planificació històrica. Per veure l'estat real i
> actualitzat, consulta [`docs/ESTAT-ACTUAL.md`](ESTAT-ACTUAL.md).

> Validat per l'Esteve el 17/07/2026. Cada tasca té UN responsable principal; ningú toca
> fitxers assignats a un altre sense avisar al xat. Actualitzeu l'estat aquí mateix.
> Estats: ⬜ pendent · 🔵 en curs · 🟡 bloquejada · ✅ feta (amb revisió creuada)

## Fase W0 — Recuperar el runner de Codex

| Tasca | Responsable | Estat | Notes |
|---|---|---|---|
| Diagnosi (ACLs, àlies MSIX, config Codex) | Claude Fable | ✅ | Causa arrel identificada: sandbox "elevated" + AzureAD → `CreateProcessAsUserW failed: 5`. Bugs upstream openai/codex#26896, #10090, #22880. Ja anotat a `~/.codex/config.toml`. |
| Aplicar el canvi a `~/.codex/config.toml` (`[windows] sandbox`) i reiniciar Codex | **Esteve** | ✅ | `sandbox = "elevated"` comentat i nova sessió iniciada. |
| Verificar que Codex torna a executar ordres i tests | Codex Sol | ✅ | Verificat: PowerShell 7.6.3, lectura del projecte, `rg` i compilació Python funcionen. |

## Fase 0 — Tests base + Telemetria W15 (Capa 1)

| Tasca | Responsable | Estat | Fitxers |
|---|---|---|---|
| Migració `collab_usage` + `collab_budget_tracker` | Codex Sol | ✅ | Renomenada a `d5e6f7a8b9c0_add_collab_usage_tables.py`, revision única i `down_revision='c1d2e3f4a5b6'`. Sintaxi i graf estàtic validats; prova Alembic real pendent de preparar l'entorn de dependències. |
| Mòdul de telemetria (`usage.py`) | Claude Fable | ✅ | `backend/open_webui/collab/usage.py`. Revisió de concurrència per Z.ai: **APROVAT**. O1 (try/except) resolt per Codex amb `_record_usage_safely()`. |
| Hooks a l'orquestrador (`_quick_completion`, `agent_turn`, `_mark_agent_down`) | Codex Sol | ✅ | Implementats i revisats per Z.ai: `_record_usage_safely()`, `_response_usage()`, telemetria a `_quick_completion`/`agent_turn`, integració amb `_mark_agent_down`/`_mark_agent_up`, `_ERROR_CONTENT_RE`. **APROVAT.** |
| Tests unitaris de classificació d'errors i agregat | Codex Sol | ✅ | `test/test_collab_usage.py`: 8 tests (classify_error 6 casos, sanitize_error_detail, estimate_tokens). **APROVAT.** |
| Revisió de concurrència (transacció log+agregat, `BEGIN IMMEDIATE`) | Z.ai.glm-5.2 | ✅ | Feta — 2 crítics a la migració (correguits per Codex), codi APROVAT. |
| Revisió conceptual de la migració i el mòdul | Codex Sol | ✅ | Migració corregida; `usage.py` compila i conserva l'aprovació de concurrència. |

> **Fase 0 TANCADA.** Totes les tasques ✅ amb revisió creuada.

## Fase 1 — W1 (visibilitat) / W9 (escolta) / W10 (conversa fluida) + hand-raise lleuger

| Tasca | Responsable | Estat | Fitxers |
|---|---|---|---|
| Disseny detallat del motor W1/W9/W10 | Z.ai.glm-5.2 | ✅ | `docs/disseny-w1-w9-w10-motor.md` — 3 taules, màquina d'estats, collab_receipt, scheduler continu, lease, envelope collab_event.v1, criteris d'acceptació. |
| Capa de dades persistent (`engine.py`) | Codex Sol | ✅ | `backend/open_webui/collab/engine.py` + migració `f6a7b8c9d0e1`. Models `CollabSession`/`CollabEvent`/`CollabReceipt`, funcions `append_event`/`create_receipts`/`update_receipt`/`acquire_lease`/`release_lease`/`supersede_handraises`. Revisat per Z.ai: sòlid. |
| Tests del motor (`engine.py`) | Codex Sol | ✅ | `test/test_collab_engine.py`: 3 tests (monotonicitat d'events + idempotència de receipts, exclusivitat de lease, idempotència de supersede). Revisat per Z.ai. |
| Integració del motor amb `orchestrator.py` | Codex Sol | ⬜ | `engine.py` existeix però `orchestrator.py` encara usa `_active_rounds` (memòria). Cal integrar lease + events + receipts. |
| Hand-raise lleuger (W15 Capa 3) + test de no-regressió | Claude Fable | ⬜ | `orchestrator.py` (coordinar amb Codex: un responsable per fitxer i moment) |

## Fase 2 — W2 (cancel·lació) / W3 (ronda única recuperable)

| Tasca | Responsable | Estat | Fitxers |
|---|---|---|---|
| Disseny detallat W2/W3 | Z.ai.glm-5.2 | ✅ | `docs/disseny-w2-w3-cancelacio-recuperacio.md` — `turn_id` + cancel·lació, timeout hard no desactivable, lease persistent a `run_round`, reconciliació d'orfes, `tool_lock`, endpoint `/turn/cancel`, criteris d'acceptació. |
| Implementació W2 (cancel·lació, timeout, endpoint) | Codex Sol | ⬜ | Depèn de Fase 1 (integració del motor). |
| Implementació W3 (lease a `run_round`, reconciliació) | Codex Sol | ⬜ | Depèn de Fase 1. |
| Botó ✖ «talla» al frontend | Claude Fable | ⬜ | `CollabAgentsBar.svelte` o xip de torn actiu. |

## Fases 3–4 — Pressupostos, perfils, modes, UI

| Tasca | Responsable | Estat | Notes |
|---|---|---|---|
| W15 Capa 2 — pressupostos actius | Z.ai.glm-5.2 (disseny ✅) / Claude Fable (codi) | 🔵 disseny ✅ | Disseny complet a `docs/disseny-w15-capa2-pressupostos.md`. Implementació depèn de Fase 0 (✅). |
| W11/W12 — `collab_profile` + `collab_channel_config` + APIs | Z.ai.glm-5.2 (model+proves ✅) / Claude Fable (codi) | 🔵 disseny ✅ | Disseny complet a `docs/disseny-w11-w12-perfils.md`. Contractes d'API tancats. Implementació a Fase 3. |
| W13/W14 — frontend (selector de modes, identitat visual) | pendent d'assignar | ⬜ | Candidat: agent nou especialitzat en Svelte/UX si l'Esteve l'afegeix. |

## Dissenyos completats per Z.ai.glm-5.2 (pendents de revisió d'equip)

| Document | Coberta | Estat |
|---|---|---|
| `docs/disseny-w11-w12-perfils.md` | collab_profile, collab_channel_config, AgentOverride, endpoints, migració | ✅ escrit, pendent de revisió |
| `docs/disseny-w15-capa2-pressupostos.md` | budget JSON, check_budget(), record_usage(), 3 estats, degradació | ✅ escrit, pendent de revisió |
| `docs/disseny-w1-w9-w10-motor.md` | 3 taules noves, màquina d'estats, scheduler, lease, envelope, criteris | ✅ escrit, pendent de revisió |
| `docs/disseny-w2-w3-cancelacio-recuperacio.md` | turn_id + cancel·lació, timeout hard, lease persistent, reconciliació, tool_lock | ✅ escrit, pendent de revisió |

## Observacions pendents (no bloquejants)

- **O1 (RESOLT):** `record_usage` sense try/except — Codex va implementar `_record_usage_safely()`.
- **O2 (backlog):** `record_usage` amb `commit=True/False` si s'usa amb sessió compartida. En la pràctica sempre es crida sense `db=`.
- **Engine.py:** en `append_event`, si `expire_on_commit=True` (default SQLAlchemy), accedir a `event.seq` després de `commit()` pot fallar. Cal verificar el comportament de `get_async_db_context` o fer `event.seq` abans del commit. No bloquejant.
- **Estàtics esborrats:** cada timeout/crash de Codex esborra els fitxers de `backend/open_webui/static/`. Patró recurrent (3a vegada). L'Esteve els ha restaurat cada vegada amb `git checkout`. No és crític però cal investigar la causa.
- **Estàtics:** l'Esteve confirma que estan restaurats (17/07/2026).

## Regles acordades (Codex Sol, 17/07/2026)
- Un responsable principal per fitxer o mòdul; avisar abans de tocar el d'un altre.
- Migracions i contractes d'API es tanquen abans que el frontend en depengui.
- Cap fase acabada sense proves; revisió creuada abans d'integrar.
- Nota git (preferència de l'Esteve): els agents NO fan commits/branques pel seu compte; l'Esteve gestiona el git.
- **Ordre d'edició d'`orchestrator.py`:** només un agent l'edita alhora. Protocol: anunciar «agafo orchestrator.py» → els altres esperen → anunciar «alliberat» quan s'acabi.
- **Criteri de bloqueig (Esteve, 17/07/2026):** no aturar la feina per incidències menors o recuperables. Només bloquejar davant risc de pèrdua de dades, seguretat, migració destructiva o conflicte real entre edicions.
