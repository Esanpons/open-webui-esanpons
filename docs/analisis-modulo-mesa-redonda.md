# Análisis crítico del módulo "Mesa Redonda" (taula rodona multi-agente)

> **Fecha:** 2026-07-19
> **Alcance:** `backend/open_webui/collab/` (21 módulos, ~7.000 líneas), `src/lib/components/collab/` + `src/lib/apis/collab/`, `test/test_collab_*.py` (16 ficheros, 227 tests).
> **Método:** revisión desde cero con cuatro ejes independientes (seguridad, concurrencia, frontend, arquitectura/tests). Los hallazgos marcados **ERROR CONFIRMADO** se han verificado leyendo el código; los tests actuales pasan (227 passed).
> **Naturaleza del código:** fork personal de Open WebUI orientado a despliegue **local mono-worker mono-usuario**. Varios riesgos "críticos en producción" son aceptables bajo ese supuesto, pero el código no impide otros despliegues. Se señala explícitamente esa distinción en cada caso.

---

## 0. Estado de implementación (actualizado tras aplicar cambios)

Se ha implementado **todo el plan §9/§11 salvo el refactor estructural opcional (MR-30/31)**. **La suite pasa: 260 tests** (227 originales + 33 nuevos: votación, contrato de clasificación de errores, y seguridad); `svelte-check` no introduce errores nuevos en los ficheros collab (los 6 errores de tipos que restan en `CollabPanel.svelte` son preexistentes y ajenos a estos cambios).

**Fase 1 — críticos y altos (backend + frontend):**

| ID | Estado | Nota |
|---|---|---|
| MR-01 Votación por `agent_id` | ✅ | `set_end_proposal(by_id=...)`; `_vote_on_proposal` excluye por id con fallback a nombre. |
| MR-02 Test de votación | ✅ | `test/test_collab_voting.py` (6 casos). |
| MR-03 Marcadores anclados | ✅ | `^FEINA_ACABADA:` / `^PLA_ACORDAT:` con `MULTILINE`. |
| MR-04 Voto = último JSON | ✅ | `_parse_vote` toma el último bloque JSON válido. |
| MR-05 Lease blindado | ✅ | Renovación con try/except + 3 reintentos; `release_lease` garantizado. |
| MR-06 I/O fuera del loop | ✅ | `snapshot` y árbol de ficheros vía `asyncio.to_thread`. |
| MR-07 Revalidar `project_dir` | ✅ | `sanitize_project_dir` en `apply_profile` e `import_profile`. |
| MR-08 Validar overrides | ✅ | `sanitize_overrides` (Pydantic `AgentOverride`) en apply/import. |
| MR-09 Cliente API 409 por status | ✅ | Clase `CollabApiError{status,detail}`. |
| MR-10 Flag `dirty` en overrides | ✅ | Aviso visual + confirmación; no se descartan ediciones. |
| MR-11 Manejo de error UI | ✅ | `removeSelected` con try/catch; panel con rama de error y "Reintenta". |
| MR-15 Techo de polling | ✅ | `_STALLED_GENERATION_TIMEOUT` independiente de `turn_timeout`. |
| MR-16 `return_exceptions` | ✅ | En handraise y votación; aviso al canal si la ronda peta. |
| MR-17 `context_messages` | ✅ | `CollabConfig.context_messages()`: 0/ausente = default. |
| MR-18 `escape_like` | ✅ | Conectado en `search_conversation`. |
| MR-19 `recent_dirs` | ✅ | Solo se devuelve a quien puede gestionar. |
| MR-20 Placeholders huérfanos | ✅ | `cleanup_orphan_turn_messages` al adquirir el lease. |

**Fase 2 — seguridad de despliegue, concurrencia, rendimiento, calidad:**

| ID | Estado | Nota |
|---|---|---|
| MR-12 Endurecer despliegue no-local | ✅ | `local_mode()` (env `COLLAB_LOCAL_MODE`, compat: sin whitelist = local). `browse`, `project_dir` y `open-vscode` exigen modo local si no hay `COLLAB_ALLOWED_ROOTS`. |
| MR-13 Gestión explícita | ✅ | `_require_channel_manager` (admin u owner) para fijar `project_dir` y `open-vscode`, con independencia de `COLLAB_ADMIN_ONLY`. |
| MR-14 Tests de seguridad/permisos | ✅ | `test_collab_security.py` +15 tests (null byte, `.git`, whitelist, `sanitize_*`, `_require_channel_manager`). |
| MR-21 Backpressure provider-first | ✅ | Adquisición provider→global; `configure()` no reconstruye con adquisiciones en vuelo. |
| MR-22 `resync` sin regresión | ✅ | Guarda `latestUserSeq >= currentSeq` antes de aplicar el REST. |
| MR-23 Poda de `rounds` | ✅ | `MAX_ROUNDS=100` en cliente + `clearCollabRounds` al desmontar la barra. |
| MR-24 Escritura peligrosa bloqueada | ✅ | `_forbidden_component` rechaza escrituras en `.git`/`IGNORED_DIRS`; `resolve_safe` rechaza null bytes. |
| MR-25 Clasificación de errores | ✅ | `classify_error` documentado como central + `test_collab_error_classification.py` (contrato con strings reales). |
| MR-26 Estado `incorporated` | ✅ | Se marca el receipt del agente que responde tras un turno con contenido. |
| MR-28 Docs corregidas | ✅ | ESTAT-ACTUAL.md (líneas orchestrator, nº módulos, nº tests) y collab-workspace.md (defaults de guardrails). |
| MR-29 A11y + dedup | ✅ | `aria-label`/`aria-pressed` en botón 🤝, Esc cierra el panel; `COLLAB_STATE_INFO` fuente única de estados. |
| MR-12b Budget ≠ caído + TTL | ✅ | Centinela `BUDGET_BLOCKED` (handraise lo trata como pass); `_models_without_collab_tools` con TTL 1h; limpieza de estado en memoria por canal al final de ronda. |
| MR-27 Código muerto | ⚠️ Parcial | Borrados `_HARD_TURN_TIMEOUT`, `fallback_role`, `import json`. **No** se borraron las funciones de `presets.py` (tienen tests; decisión conservadora). |
| MR-30/31 Refactor estructural | ⬜ No hecho | `TurnExecutor`/`RoundScheduler`/`CollabRuntime` y fuente única de config: cambios grandes de arquitectura, deliberadamente fuera de alcance para no arriesgar regresiones en un fork estable. Ver §10. |

**Bugs adicionales encontrados al implementar** (no estaban en el informe original):
1. El `finally` de `run_round` usaba `suppress(asyncio.CancelledError)`; al ampliarlo a `Exception` para MR-05 se vio que **ninguna por separado** bastaba —`CancelledError` hereda de `BaseException`, no de `Exception`—, saltándose `release_lease`. Corregido con `suppress(asyncio.CancelledError, Exception)`.
2. Nota de compatibilidad de MR-12: `local_mode()` se resuelve a `True` cuando **no** hay `COLLAB_ALLOWED_ROOTS`, preservando el comportamiento histórico (sin whitelist se asumía despliegue local). Para endurecer un despliegue compartido hay que definir `COLLAB_ALLOWED_ROOTS` y dejar `COLLAB_LOCAL_MODE` sin activar.

---

## 1. Resumen ejecutivo

El módulo implementa una "mesa redonda" de IAs **sin director**: tras cada mensaje se pregunta a cada agente si quiere intervenir (hand-raising), hablan por prioridad en turnos secuenciales, y la ronda termina por consenso (votación sobre `FEINA_ACABADA` / `PLA_ACORDAT`), por silencio o por guardarail. Comparten un proyecto de ficheros en disco, un tablero de tareas y un resumen incremental, todo persistido en BD con eventos de secuencia monótona y leases.

**Es un módulo notablemente por encima de la media para un proyecto de fork personal.** La capa de persistencia (`engine.py`) es sólida: leases con TTL y CAS, eventos idempotentes, receipts, upserts dialecto-agnósticos, versionado optimista. Las escrituras a disco son atómicas (tempfile + fsync + `os.replace`). Los fallos de un agente no tumban la ronda. Hay 227 tests que corren contra SQLite sin servicios externos.

**Pero arrastra cuatro debilidades estructurales:**

1. **Un modelo de decisión frágil.** El consenso depende de que los LLM emitan literales de texto exactos en catalán (`FEINA_ACABADA:`) parseados con regex sin anclar, y de una votación que **puede contar el voto del propio proponente** (error confirmado, §4.1).
2. **Dos modelos de concurrencia contradictorios.** Se paga toda la complejidad de un diseño durable multi-worker (leases, reconciliación) mientras cinco diccionarios en memoria lo atan a un solo worker; con 2+ workers, `stop`/`cancel` fallan de forma no determinista.
3. **La superficie de seguridad está desactivada por defecto.** El control de acceso descansa en el modelo de canales de Open WebUI y en dos variables de entorno (`COLLAB_ADMIN_ONLY`, `COLLAB_ALLOWED_ROOTS`) que **por defecto no restringen nada**. Sin ellas, cualquier gestor de un canal puede fijar `project_dir` a cualquier carpeta del host, listar todo el sistema de ficheros (`/browse`), escribir ficheros ejecutables y lanzar VS Code en el servidor. Hay además un **bypass confirmado** de la (débil) whitelist vía import/apply de perfiles.
4. **Complejidad concentrada y poco testeada donde importa.** `agent_turn` (~350 líneas) y `run_round` (~295 líneas) son funciones-dios; el consenso (`voting.py`), el router HTTP y `commands.py` no tienen tests; el refactor "modular" W7 es cosmético (dependencias circulares vía import tardío).

**Veredicto:** para el uso declarado (local, un usuario, un worker) el módulo es funcional y razonablemente robusto. Los arreglos de mayor ROI son pequeños y localizados: el bug de votación, anclar los marcadores de consenso, conectar `escape_like`, blindar el ciclo de vida del lease, y revalidar `project_dir` en el camino de perfiles. La deuda estructural (fuente única de config, romper el ciclo del orchestrator, tests del consenso/router) es más grande pero no urgente si el despliegue sigue siendo local.

---

## 2. Puntos fuertes (para ser justos)

- **`engine.py` es la mejor pieza:** eventos con secuencia monótona transaccional, dedupe de mensajes humanos con unique constraint, leases con CAS (`owner IS NULL OR expirado OR == owner`), receipts idempotentes. Tests de integración reales contra SQLite (50 inserts gapless, dedupe entre workers, expiración con monkeypatch de `time`).
- **Escritura atómica de ficheros del proyecto** con `tempfile.mkstemp` + `os.replace` + límite de tamaño + limpieza de temporales huérfanos tras un crash.
- **Anti path-traversal sólido *dentro* de `project_dir`:** `resolve_safe` resuelve symlinks y compara `parents`; tests cubren `..`, absolutas fuera, symlinks fuera y separadores mixtos.
- **Aislamiento de fallos por agente:** excepciones capturadas por turno, agentes caídos saltados con reintento automático a los 5 min, circuit breaker con backoff, backpressure con semáforos global + por proveedor.
- **Frontend disciplinado en ciclos de vida:** todas las suscripciones a socket, intervalos y debounces se limpian al desmontar (con guard `destroyed`). No hay `@html` ni superficie XSS real (contenido de agentes y resumen se renderizan como texto plano).
- **Versionado optimista (CAS sobre `meta_version`)** para `channel.meta`, con 409 y reconciliación en el frontend.

---

## 3. Cómo leer las prioridades

| Prioridad | Criterio |
|---|---|
| 🔴 **Crítica** | Rompe el mecanismo central (decisión/consenso) o abre RCE/escritura arbitraria en el escenario esperado. |
| 🟠 **Alta** | Bug confirmado con impacto en uso normal, o riesgo de seguridad serio en despliegue no-local, o deuda que ya ha causado bugs. |
| 🟡 **Media** | Comportamiento incorrecto en casos límite, o deuda que causará bugs pronto. |
| 🟢 **Baja** | Pulido, código muerto, coherencia, a11y/i18n. |

Clasificación de cada hallazgo: **ERROR CONFIRMADO** / **RIESGO POTENCIAL** / **MEJORA RECOMENDADA** / **DUDA** (no verificable solo con el código).

---

## 4. Errores confirmados

### 4.1 🔴 El proponente puede votar su propia propuesta de cierre — CONFIRMADO

- **Qué:** `agent_turn` registra la propuesta con `by = name`, donde `name = _agent_display_name(resolved, model, agent_id)`, es decir el **display_name del override** si existe ([orchestrator.py:975](backend/open_webui/collab/orchestrator.py#L975), [orchestrator.py:1296-1301](backend/open_webui/collab/orchestrator.py#L1296-L1301)). Pero `_vote_on_proposal` excluye al proponente comparando con el **nombre crudo del modelo**: `models.get(a, {}).get("name", a) != proposer` ([voting.py:25-27](backend/open_webui/collab/voting.py#L25-L27)).
- **Por qué es un problema:** con la funcionalidad de alias personalizados (W12) activada —que la documentación presenta como caso de uso principal—, `display_name ≠ model.name`, el filtro nunca coincide, y el proponente entra en la lista de votantes y previsiblemente vota a favor de sí mismo.
- **Impacto:** el consenso se sesga; con pocos agentes, el voto del propio proponente puede decidir el cierre → **rondas cerradas prematuramente**. Rompe la regla explícita del propio docstring ("El proposant no vota"), y precisamente en la configuración recomendada.
- **Cómo mejorarlo:** guardar `agent_id` en la propuesta (`set_end_proposal(..., by_id=agent_id)`) y filtrar votantes por id, no por nombre. Comparar por identificador estable en todo el flujo de consenso.
- **Ubicación:** [voting.py:25-27](backend/open_webui/collab/voting.py#L25-L27), [orchestrator.py:1292-1302](backend/open_webui/collab/orchestrator.py#L1292-L1302), [tasks.py](backend/open_webui/collab/tasks.py) (`set_end_proposal`).

### 4.2 🟠 Bypass de la validación de `project_dir` vía import/apply de perfiles — CONFIRMADO

- **Qué:** el camino config→panel (`update_config`) valida `project_dir` con `validate_project_dir` (whitelist `COLLAB_ALLOWED_ROOTS`). Pero el camino perfil→canal **no revalida**: `apply_profile` copia `space_config` (con su `project_dir`) y lo sincroniza a `channel.meta.collab` vía `_sync_channel_meta` sin pasar por `validate_project_dir` ([profiles.py:485](backend/open_webui/collab/profiles.py#L485), [profiles.py:502](backend/open_webui/collab/profiles.py#L502)). `validate_imported_profile` solo valida tipos de nivel superior, no el contenido de `config` ([profiles.py:342-369](backend/open_webui/collab/profiles.py#L342-L369)).
- **Por qué es un problema:** importar un perfil o aplicar una plantilla con un `project_dir` a una carpeta sensible evade la whitelist. El motor consume después `channel.meta.collab.project_dir` como carpeta canónica sin haberlo validado nunca.
- **Impacto:** en despliegue no-local, escalada directa: fijar `project_dir` a una carpeta arbitraria (con las implicaciones de §5.1/§5.2) saltándose el único control existente.
- **Cómo mejorarlo:** revalidar `project_dir` (y guardrails) con las mismas reglas de `update_config` dentro de `apply_profile`, `update_profile` (canales vinculados) e `import_profile`; validar `config` importado contra un esquema estricto (no `dict` libre).
- **Ubicación:** [profiles.py:342-369](backend/open_webui/collab/profiles.py#L342-L369), [profiles.py:457-508](backend/open_webui/collab/profiles.py#L457-L508), [router.py:214-221](backend/open_webui/collab/router.py#L214-L221).

### 4.3 🟠 La capa de validación de overrides existe pero nunca se ejecuta — CONFIRMADO

- **Qué:** `AgentOverride` (Pydantic con `priority ge=1 le=5`) y `_validate_overrides()` ([profiles.py:74-129](backend/open_webui/collab/profiles.py#L74-L129)) **no se usan en ningún sitio** (verificado por grep en backend/ y src/). `ProfileForm.agent_overrides` es `list[dict]` sin validar, y ningún endpoint llama a `_validate_overrides`.
- **Por qué es un problema:** la API acepta overrides arbitrarios (`effort: "banana"`, `priority: 999`, `model_id` inexistente) que llegan intactos a `resolve_agent()` y al `form_data` del proveedor. El log "Override ignorat" que promete la función no ocurre jamás.
- **Impacto:** fallos en runtime lejos del origen — p. ej. `reasoning_effort` inválido rechazado por el proveedor y **clasificado como caída del agente** (el circuit breaker penaliza a un agente sano).
- **Cómo mejorarlo:** tipar `ProfileForm.agent_overrides: list[AgentOverride]` y llamar a `_validate_overrides` en `apply_profile`/`update_channel_config`; o eliminar ambas cosas si se decide no validar.
- **Ubicación:** [profiles.py:74-129](backend/open_webui/collab/profiles.py#L74-L129).

### 4.4 🟡 `escape_like` está testeado pero nunca se conecta; la búsqueda real no escapa — CONFIRMADO

- **Qué:** `search_conversation` hace `Message.content.ilike(f"%{query}%")` con el `query` del usuario/agente sin `escape_like` ni `ESCAPE` ([history.py:100](backend/open_webui/collab/history.py#L100)). `escape_like` existe y tiene 4 tests ([files.py:60](backend/open_webui/collab/files.py#L60)), pero no se invoca en ningún sitio.
- **Por qué es un problema:** los comodines `%` y `_` del input actúan como wildcards. No es SQLi (SQLAlchemy parametriza el valor), pero permite consultas patológicas que degradan la BD, y el test verde da falsa sensación de mitigación aplicada.
- **Impacto:** DoS leve / abuso de recursos de BD; deuda engañosa.
- **Cómo mejorarlo:** `Message.content.ilike(f"%{escape_like(query)}%", escape="\\")` en [history.py:100](backend/open_webui/collab/history.py#L100).
- **Ubicación:** [history.py:100](backend/open_webui/collab/history.py#L100), [files.py:60](backend/open_webui/collab/files.py#L60).

### 4.5 🟡 `context_messages=0` produce 30, no "desactivado" — CONFIRMADO

- **Qué:** `build_transcript` hace `int(config.guardrail("context_messages") or 30)` ([context.py:21](backend/open_webui/collab/context.py#L21)). El default real es 15 ([config.py:37](backend/open_webui/collab/config.py#L37)) y la doc dice "0 = desactivat per als numèrics".
- **Por qué es un problema:** tres valores para el mismo concepto (0→30 por el `or`, default 15, doc 30), y el contrato "0 = off" se viola: poner 0 *duplica* el contexto. El fallback 30 es un magic number duplicado en handraise ([orchestrator.py:854](backend/open_webui/collab/orchestrator.py#L854)).
- **Impacto:** comportamiento sorprendente y coste de tokens no intencionado.
- **Cómo mejorarlo:** un único helper `effective_context_messages(config)` con semántica definida para 0.
- **Ubicación:** [context.py:21](backend/open_webui/collab/context.py#L21), [orchestrator.py:854](backend/open_webui/collab/orchestrator.py#L854), [config.py:37](backend/open_webui/collab/config.py#L37).

### 4.6 🟡 Fuga de estado global `recent_dirs` entre canales — CONFIRMADO

- **Qué:** `get_config` devuelve `recent_dirs` a cualquier miembro con permiso de **lectura** ([router.py:590-606](backend/open_webui/collab/router.py#L590-L606)), y `get_recent_dirs` es **global a todo el sistema**, no por canal ([config.py:161-180](backend/open_webui/collab/config.py#L161-L180)).
- **Por qué es un problema:** un usuario de solo lectura de un canal ve rutas absolutas del host usadas en **otros** canales/proyectos (estado transversal compartido vía `SystemConfig`).
- **Impacto:** divulgación de rutas del host y de actividad de otros espacios.
- **Cómo mejorarlo:** no devolver `recent_dirs` a no-admins / sin permiso de gestión; considerar ocultar la ruta absoluta a solo-lectura.
- **Ubicación:** [router.py:590-606](backend/open_webui/collab/router.py#L590-L606), [config.py:161-180](backend/open_webui/collab/config.py#L161-L180).

### 4.7 🟠 Frontend: manejo de conflicto 409 acoplado al texto del error en catalán — CONFIRMADO

- **Qué:** el cliente API descarta el status HTTP (`error = err.detail ?? err`, un string) ([index.ts:16-28](src/lib/apis/collab/index.ts#L16-L28)). `CollabPanel.save()` detecta el conflicto de versión con `msg.includes('canviat') || msg.includes('Refresca')`, acoplado literalmente al texto de `router.py`.
- **Por qué es un problema:** el contrato front↔back es el *texto humano* del error, no el código. Cualquier cambio de redacción o traducción rompe silenciosamente la reconciliación automática (el usuario vería un toast genérico en vez del refresco). En `CollabProfiles.persistOverrides` el 409 ni siquiera tiene rama amable.
- **Impacto:** manejo de concurrencia frágil; pérdida silenciosa de la rama de reconciliación.
- **Cómo mejorarlo:** que `request()` lance `{ status, detail }` (o clase `CollabApiError`) y comparar `e.status === 409`.
- **Ubicación:** [index.ts:5-31](src/lib/apis/collab/index.ts#L5-L31), `CollabPanel.svelte:255-260`.

### 4.8 🟠 Frontend: ediciones de "Personalización por agente" se pierden silenciosamente — CONFIRMADO

- **Qué:** los inputs de overrides mutan `effective` solo en memoria; cualquier `load()` (colapsar/expandir Plantillas vía `toggle()`, aplicar plantilla, importar) refetchea `effective` y descarta lo editado sin aviso ni flag `dirty`.
- **Por qué es un problema:** patrón de pérdida de datos — el usuario escribe un system prompt largo, pliega/despliega la sección y desaparece.
- **Impacto:** pérdida de trabajo del usuario.
- **Cómo mejorarlo:** flag `dirty` (comparar con snapshot), no refetchear en `toggle` si hay cambios, o autosave con debounce como el resto del panel.
- **Ubicación:** `CollabProfiles.svelte:63-66` (`toggle`), `203-210` (`updateOverride`), `36-55` (`load`).

### 4.9 🟡 Frontend: acciones sin manejo de error / panel sin estado de error — CONFIRMADO

- **Qué:** (a) `removeSelected` llama `deleteCollabProfile` sin `try/catch` → *unhandled rejection* y UI a medias (`CollabProfiles.svelte:95-98`). (b) `CollabPanel` no tiene rama `{:else}`: si `loadConfig` falla, queda una cabecera con cuerpo en blanco, sin mensaje ni reintento (`CollabPanel.svelte:463-465`).
- **Impacto:** el usuario no sabe si una acción falló; callejón sin salida visual.
- **Cómo mejorarlo:** `try/catch` + `toast.error` en todas las acciones; rama de error con botón "Reintentar".
- **Ubicación:** `CollabProfiles.svelte:95-98`, `CollabPanel.svelte:463-465`.

### 4.10 🟢 Restos confirmados menores

- **Estado de receipt `"incorporated"` inalcanzable:** está en `RECEIPT_STATES` ([engine.py:24](backend/open_webui/collab/engine.py#L24)) pero ningún código lo asigna; los tests lo verifican siempre a 0 → media funcionalidad W9 muerta.
- **`presets.py`: ~mitad muerto** — `preset_to_profile_form()`, `extract_mode_from_config()`, `extract_guardrails_from_config()` sin callers; el docstring promete un `resolve_preset()` que no existe.
- **`reset_channel_to_defaults` ignora el flag de éxito** de la tupla `update_channel_config` ([router.py:367-372](backend/open_webui/collab/router.py#L367-L372)): ante conflicto de versión respondería 200 con datos stale.
- **Documentación desactualizada:** ESTAT-ACTUAL.md dice "orchestrator.py ~640 líneas" (son 1.602) y "18 módulos" (son 21); collab-workspace.md documenta defaults de guardrails equivocados (`auto_summary` off→es on, `context_messages` 30→es 15). Confunde a quien mantenga.

---

## 5. Riesgos potenciales

### Seguridad (crítico solo en despliegue no-local; aceptable en local mono-usuario)

#### 5.1 🟠 `/browse` permite recorrer todo el sistema de ficheros del servidor — RIESGO

Sin `COLLAB_ALLOWED_ROOTS`, `_default_roots()` devuelve todas las unidades (Windows) o `/` (POSIX) y `validate_project_dir` solo se aplica *cuando hay* allowed_roots ([router.py:888-928](backend/open_webui/collab/router.py#L888-L928)). Un gestor puede listar cualquier carpeta del host. **Mejora:** exigir `COLLAB_ALLOWED_ROOTS` para habilitar `/browse` y `project_dir` fuera de modo local; denegar `/browse` sin roots.

#### 5.2 🟠 Lectura/escritura de ficheros arbitrarios del host vía `project_dir` sin whitelist real — RIESGO

Con `COLLAB_ALLOWED_ROOTS` vacío, `validate_project_dir` solo comprueba que la carpeta exista y que el usuario sea admin ([config.py:196-219](backend/open_webui/collab/config.py#L196-L219)). `resolve_safe` impide salir de la raíz, pero **la raíz puede ser toda la máquina**. Los modelos (incluidos pipes CLI) reciben tools de escritura sobre ella. Si `project_dir` apunta a `/` o al home del servicio, la escritura dirigida por LLM equivale a RCE (crontab, `.bashrc`, hooks de git). **Mejora:** hacer `COLLAB_ALLOWED_ROOTS` obligatorio para fijar `project_dir`; prohibir raíces peligrosas; auditar cada escritura por agente.

#### 5.3 🟡 Escritura de ejecutables / dentro de `.git/` no bloqueada — RIESGO

`write_text_file` solo limita tamaño; no hay whitelist de extensiones ni bloqueo de `.git/`. `IGNORED_DIRS` solo afecta al *listado/snapshot*, no a `resolve_safe`/escritura. Un agente puede sobrescribir `.git/hooks/pre-commit` → RCE al siguiente commit local. **Mejora:** rechazar en `resolve_safe` rutas cuyo primer componente esté en `IGNORED_DIRS`; considerar whitelist de extensiones de escritura.

#### 5.4 🟡 `open-vscode` lanza un proceso GUI en el servidor desde una petición HTTP — RIESGO

`subprocess.Popen([code_bin, "-n", project_dir])` con `shell=False` (no es inyección), pero es una primitiva de ejecución de procesos expuesta por API, protegida solo por rol de canal ([router.py:758-789](backend/open_webui/collab/router.py#L758-L789)). Asume backend=escritorio. **Mejora:** condicionar a una flag explícita de "modo local", no solo al rol.

#### 5.5 🟠 En canales públicos con escritura pública, cualquier usuario verificado gestiona el espacio — RIESGO

`_get_channel_checked` usa `channel_has_access(..., strict=False)` ([router.py:101-102](backend/open_webui/collab/router.py#L101-L102)); con un grant de escritura pública, todo usuario verificado puede reconfigurar la mesa, cambiar `project_dir`, arrancar rondas y escribir ficheros, sujeto solo a `_check_can_manage` (que solo actúa si `COLLAB_ADMIN_ONLY` está activo). Amplifica §5.1–5.3. **Mejora:** para operaciones sensibles (config de `project_dir`, start, open-vscode) exigir gestión explícita (owner/admin) con independencia de `COLLAB_ADMIN_ONLY`.

#### 5.6 🟡 Los turnos se ejecutan con la identidad de quien disparó la ronda + `bypass_filter=True` — DUDA

`_get_models` y `generate_chat_completion(..., bypass_filter=True)` usan el `user` que inició la ronda ([orchestrator.py:645-649](backend/open_webui/collab/orchestrator.py#L645-L649), [orchestrator.py:1080-1083](backend/open_webui/collab/orchestrator.py#L1080-L1083)); la auto-activación convierte al primer humano en "propietario". En canal compartido, el conjunto de modelos y el salto de filtros quedan atados a un usuario que puede no ser el adecuado, y dificulta auditar quién ejecutó qué. **No verificable solo con el código** (depende de la semántica exacta de `bypass_filter` y del modelo de amenazas). **Mejora:** documentar/acotar la identidad de ejecución; considerar una identidad de servicio.

### Concurrencia y robustez

#### 5.7 🟠 Renovación de lease sin manejo de errores → posible doble ronda — RIESGO (parte CONFIRMADO)

`_renew_round_lease` llama `renew_lease` sin try/except: una excepción transitoria de BD mata la tarea en silencio y la ronda sigue sin renovar; el lease (TTL 30 s) expira y otra invocación puede adquirirlo → **dos rondas sobre el mismo canal** (escrituras cruzadas de ficheros, transcripts entrelazados). Además, en el `finally` de `run_round`, `await lease_task` solo suprime `CancelledError`: si la tarea murió con otra excepción, se **relanza y se salta `release_lease`** (esto último es error confirmado por el flujo de excepciones). **Ubicación:** [orchestrator.py:607-620](backend/open_webui/collab/orchestrator.py#L607-L620), [orchestrator.py:1592-1602](backend/open_webui/collab/orchestrator.py#L1592-L1602). **Mejora:** try/except con reintentos en la renovación y `state["stop"]`+`cancel_turn` al fallar; `suppress(Exception)` al esperar `lease_task` para garantizar `release_lease`.

#### 5.8 🟠 I/O de disco síncrona en el event loop — ERROR CONFIRMADO

`snapshot()` (hasta 20.000 `stat`) se llama en línea en `agent_turn` ([orchestrator.py:1077](backend/open_webui/collab/orchestrator.py#L1077), [orchestrator.py:1237](backend/open_webui/collab/orchestrator.py#L1237)), y `tree_as_text`/`build_tree` son síncronos al componer el prompt ([context.py:76](backend/open_webui/collab/context.py#L76)). Solo `cleanup_temp_files` usa `to_thread`. En carpetas grandes bloquea el loop entero (sockets, renovación de lease → refuerza §5.7, resto de peticiones del servidor). **Mejora:** `await asyncio.to_thread(snapshot, ...)` y árbol en thread.

#### 5.9 🟠 Bucle de polling potencialmente infinito con `turn_timeout=0` — RIESGO

`_run_generation_until_done` hace polling cada 1,5 s hasta `meta.done`; la única red de seguridad es el `wait_for` externo, que es `None` (sin límite) cuando `turn_timeout=0` (feature documentada). Si el pipeline muere sin marcar `done`, la tarea gira para siempre **reteniendo el slot de backpressure** → agotamiento de semáforos → parálisis de todos los canales. **Ubicación:** [orchestrator.py:399-404](backend/open_webui/collab/orchestrator.py#L399-L404), [turns.py:68-70](backend/open_webui/collab/turns.py#L68-L70). **Mejora:** techo absoluto de sanidad o detección de "mensaje sin progreso" independiente del guardrail.

#### 5.10 🟡 `asyncio.gather` sin `return_exceptions=True` en handraise y votación — RIESGO

[orchestrator.py:891](backend/open_webui/collab/orchestrator.py#L891) y [voting.py:70](backend/open_webui/collab/voting.py#L70): si una corrutina lanza algo inesperado, `gather` propaga la primera excepción y **deja el resto corriendo sin observar**; la ronda muere capturada solo con log (§5.14). **Mejora:** `return_exceptions=True`, tratar excepción como estado "error" por agente, y avisar al canal cuando `run_round` peta.

#### 5.11 🟡 Backpressure: head-of-line blocking y `configure()` en caliente — RIESGO

`acquire` toma el semáforo **global antes** del de proveedor ([backpressure.py:127-139](backend/open_webui/collab/backpressure.py#L127-L139)): con un proveedor saturado, hasta 10 llamadas consumen el cupo global entero (inanición de otros proveedores). `configure()` reconstruye los semáforos con adquisiciones en vuelo → releases a objetos antiguos, límites transitoriamente superados. **Mejora:** adquirir provider primero (o `wait_for` corto), no reconstruir en caliente.

#### 5.12 🟡 Reinicio a mitad de ronda: sin reanudación ni limpieza de placeholders — RIESGO (placeholder huérfano CONFIRMADO)

Tras un crash, nada reanuda la ronda (`reconcile_channel` solo se llama desde `/start`), y los mensajes "⏳ *treballant…*" con `meta.done=False` quedan huérfanos: la UI muestra un agente "hablando" eternamente ([orchestrator.py:994-1004](backend/open_webui/collab/orchestrator.py#L994-L1004)). **Mejora:** al arrancar / adquirir lease expirado, marcar cancelados los mensajes de agente sin `done`; opcionalmente reanudar rondas con `user_message` sin consumir.

#### 5.13 🟡 TOCTOU de presupuesto y de `down_agents` — RIESGO (bajo)

`check_budget` decide antes de que las N llamadas paralelas registren su consumo → el límite puede excederse en hasta N quick-calls (acotado por `max_tokens`). `set_down_agent`/`clear_down_agent` hacen read-modify-write del dict entero sin CAS → un `/agents/retry` concurrente con `_mark_agent_down` puede pisar la escritura (se autocorrige en el siguiente ciclo). Los **contadores en sí no se corrompen** (incremento SQL atómico). **Mejora:** reserva de tokens si se quiere presupuesto estricto; una fila por agente en `collab_state`.

### Frontend (rendimiento/estado)

#### 5.14 🟡 Regresión de ronda en `resync()` — RIESGO

`resync()` captura `latestUserSeq` al inicio, hace varios `await` y al final sobrescribe `currentSeq`/`agentStates`/`summary`; si entre medias llega por socket un `user_message` nuevo, el final de `resync` pisa la ronda nueva con la vieja (hasta 90 s, `CollabAgentsBar.svelte:180-215`). **Mejora:** comprobar `latestUserSeq >= currentSeq` antes de aplicar el resultado REST.

#### 5.15 🟡 Cada montaje de la barra re-descarga TODO el historial desde seq 0 — RIESGO

`lastSeq` empieza en 0 y `list_events` **no tiene poda ni retención** ([engine.py:462](backend/open_webui/collab/engine.py#L462)): en un canal veterano, cada cambio de canal reproduce O(historial) peticiones y `rounds`/`seqToMessageId` crecen sin límite. **Mejora:** endpoint de snapshot (última ronda + N últimas) y poda de `rounds`.

#### 5.16 🟢 Otros riesgos frontend menores

- Rondas pintadas con lista de agentes cacheada (config refrescada cada 90 s) → chips desactualizados hasta el próximo resync.
- Inyección de CSS vía `identity.color` sin validar (`red; background:url(...)`) — no es XSS (Svelte escapa el atributo), defacement en el peor caso. Validar `/^#[0-9a-f]{3,8}$/i`.
- Flujos multi-paso no atómicos en plantillas (`persistOverrides→save→apply→load`) con read-then-write de versión (TOCTOU) que neutraliza parcialmente el CAS del backend.

### Manejo de errores

#### 5.17 🟡 `except Exception` de la ronda sin aviso al usuario — MEJORA

`run_round` envuelve el bucle en `except Exception: log.exception(...)` sin `post_notice` ([orchestrator.py:1588-1589](backend/open_webui/collab/orchestrator.py#L1588-L1589)): si la ronda peta por un bug nuestro, el canal se queda mudo y el usuario no sabe si sigue trabajando. Contrasta con los otros ~10 caminos de error que sí publican aviso. **Mejora:** `post_notice("💥 La ronda ha fallat internament: …")`.

#### 5.18 🟡 Política fail-open sistemática sin señal agregada — RIESGO

`_circuit_allows`, `_record_circuit_result`, `_validate_models`, `_resolved_agent`, `_channel_budget`, `get_collab_config`… todos capturan `except Exception`, loggean y continúan permisivos. Individualmente defendible (y los logs son buenos), pero en conjunto: si `collab_state` se corrompe, se pierden a la vez circuit breaker, budget y overrides sin ninguna señal agregada; `get_collab_config` ante meta corrupta devuelve `enabled=False` → **la mesa se apaga en silencio**. **Mejora:** contador de fail-opens por canal en `/config`, o aviso al primer fail-open de cada categoría.

---

## 6. Mejoras recomendadas (arquitectura, mantenibilidad)

### 6.1 🟠 El protocolo de consenso depende de literales de texto sin anclar

Consenso y fases dependen de que el modelo emita exactamente `FEINA_ACABADA:`/`PLA_ACORDAT:`/`ESPEREM_USUARI` ([orchestrator.py:119-121](backend/open_webui/collab/orchestrator.py#L119-L121)), y el voto se parsea con `'"agree"\s*:\s*(true|false)'` sobre texto libre tomando **la primera** aparición ([voting.py:34-67](backend/open_webui/collab/voting.py#L34-L67)). `_FINISH_MARKER_RE` con `re.DOTALL` captura desde cualquier posición: un agente que *cite* el marcador dispara una propuesta de cierre. El propio `agents_status.py` documenta que este mismo problema (falsos positivos por mención) ya obligó a anclar el regex de errores — pero los marcadores de consenso siguen sin anclar. **Mejora:** anclar a inicio de línea final (`^FEINA_ACABADA:` con MULTILINE sobre las últimas líneas), parsear el **último** bloque JSON válido en el voto, y hacer de las tools (`propose_finish`, un `vote()`) el camino primario dejando los marcadores como fallback anclado para pipes CLI.

### 6.2 🟠 El refactor W7 es cosmético: ciclo orchestrator ↔ módulos extraídos

`voting.py` importa `_quick_completion` de orchestrator dentro de las funciones; `agents_status.py` importa `post_notice`; `file_tools.py` importa `lock_turn_tool`/`unlock_turn_tool` **de orchestrator** (que solo los re-exporta de `turns.py`). La dependencia es circular de facto (import tardío = fallo en runtime, no en import); ningún módulo extraído es testeable sin orchestrator cargado — de ahí que voting/agents_status no tengan tests. **Mejora:** inyectar `completion_fn`/`notify_fn` (o un protocolo `CollabRuntime`); `file_tools` debe importar de `turns` directamente.

### 6.3 🟠 Tres fuentes de configuración sincronizadas a mano

La config vive en (1) `channel.meta['collab']` (canónica del motor), (2) `collab_channel_config` (overrides + budget + vínculo a plantilla) y (3) `collab_profile`. La coherencia depende de llamar siempre en pareja `save_collab_config()` + `sync_channel_config_from_meta()` y de `_sync_channel_meta()` en sentido inverso, con **dos esquemas de versión distintos** (`meta_version` y `collab_channel_config.version`) que no se protegen mutuamente. El propio docstring confiesa que la divergencia ya "aturava silenciosament el motor". **Mejora:** una sola tabla propietaria (`collab_channel_config`) como fuente de verdad; `channel.meta.collab` como caché derivada escrita en la misma transacción, o eliminada.

### 6.4 🟠 `agent_turn` (~350 líneas) y `run_round` (~295 líneas) son funciones-dios

`agent_turn` mezcla resolución de overrides, budget, placeholder, composición de prompt, backpressure + 3 estrategias de retry (closure de 80 líneas), cancelación, diff de ficheros, clasificación de errores, telemetría, circuit breaker y detección de marcadores. Son imposibles de testear sin 12-17 monkeypatches (§7). **Mejora:** extraer `TurnExecutor` (prompt + retries + telemetría) y `RoundScheduler` (selección de speaker por modo); las closures anidadas son el síntoma.

### 6.5 🟡 Clasificación de errores por regex dispersa y acoplada al wording del proveedor

Cinco regex heurísticos repartidos entre `orchestrator.py`, `usage.py` y `agents_status.py` (este último acoplado al formato Markdown exacto de los pipes propios, `**Claude error:**`). Cada proveedor nuevo o cambio de wording rompe la clasificación en silencio (retry incorrecto, agente sano marcado caído). **Mejora:** centralizar en `usage.classify_error` con un test de contrato contra los strings reales de los pipes de `integrations/`.

### 6.6 🟡 Magic numbers que contradicen la filosofía declarada

`config.py` declara "Mai apliquem límits fixos al codi", pero el código fija ~12 umbrales dispersos en 4 ficheros (2 fallos → caído, `_RETRY_DOWN_SECONDS=300`, 2 nudges, poll 1,5 s, lease 30/10, `<=2` mensajes para auto-activar, contexto degradado=5, `max_entries=80`…). **Mejora:** agruparlos en un `EngineTunables` en config.py con nombre y comentario, y corregir la afirmación.

### 6.7 🟢 Otras mejoras

- **`_models_without_collab_tools` nunca se revalida** hasta reiniciar: un modelo que gane soporte de tools sigue vetado. Añadir TTL.
- **Pausa por presupuesto disfrazada de "agente caído":** `_quick_completion` devuelve `None` y handraise lo cuenta como error → agente sano marcado caído con mensaje engañoso. Estado diferenciado `budget_blocked` tratado como `pass`.
- **Boilerplate transaccional repetido 15+ veces** en engine.py (`if owns_session: commit else: flush`). Mover a `_session_scope`.
- **`create_task(run_round(...))` sin retener referencia** ([router.py:714](backend/open_webui/collab/router.py#L714), 753): el GC puede recolectar la tarea. Guardar en un set con `add_done_callback`.
- **API REST inconsistente:** unos endpoints usan body Pydantic, otros query params para datos de negocio; `GET /channel-config` **escribe** en BD (lazy migration) sin exigir permiso de escritura.
- **i18n inexistente:** los 5 componentes y los mensajes del backend están hardcodeados en catalán; el protocolo LLM (marcadores, prompts de fase/voto) está en catalán, lo que puede degradar modelos pequeños/locales que el proyecto dice querer soportar. Decisión probablemente deliberada del fork, pero conviene documentarla y separar los strings de protocolo.
- **A11y:** botón 🤝 sin `aria-label`, `role="status"` sobre contenido que muta constantemente (verboso para lectores), ayuda de guardrails solo en `title`, panel que no cierra con Esc pese al comentario que lo promete.
- **Duplicación de constantes de estado** (`STATE_INFO` vs `STATE_ICONS`+`STATE_LABELS`) y `modelName` definido 3 veces en el frontend.

---

## 7. Tests

**Inventario:** 16 ficheros, 227 tests, **todos en verde** (verificado: `227 passed in 18.53s` con `WEBUI_SECRET_KEY` y `PYTHONPATH=backend`). Corren contra SQLite sin servicios externos.

**Lo bueno:** `test_collab_engine.py` es la joya — integración real contra aiosqlite, concurrencia (50 inserts gapless, dedupe entre workers), idempotencia y expiración de lease con monkeypatch de `time`. Circuit breaker con ciclo completo contra BD real.

**Zonas sin ningún test (huecos verificados):**
- **Endpoints del router** — solo se testea el helper `_validate_models`; ningún `TestClient` (permisos, 409, apply de presets/perfiles vía HTTP, `open-vscode`, `browse` sin allowed_roots).
- **`voting.py`** — el mecanismo de consenso, donde vive el bug §4.1. Un solo test de `_vote_on_proposal` con override lo habría atrapado.
- **`commands.py`** (246 líneas, 0 imports en tests), **`context.py`/`build_transcript`** (siempre mockeada, nunca ejecutada), **`handle_collab_message`** (el punto de entrada, con la auto-activación §5.x), **`history.py`**, **`prompts._phase_block`**.

**Fragilidad:** `test_collab_orchestrator_w5_w15` parchea **17 atributos** internos de orchestrator (incluidas funciones privadas): son tests de *cableado interno*, no de comportamiento; cualquier refactor (§6.4) rompe la suite aunque el comportamiento externo sea idéntico. Mockear `build_transcript` en todos los tests significa que un bug en ella no lo detecta nadie.

**Cobertura de seguridad muy estrecha:** `test_collab_security.py` solo prueba `resolve_safe` y `escape_like` (esta última sin uso real, §4.4). No cubre autorización de endpoints, IDOR entre canales, el bypass §4.2, `validate_project_dir` con/sin allowed_roots, ni escritura en `.git/`.

**Mejoras de test prioritarias:**
1. `_vote_on_proposal` con override de nombre (barato, mismo patrón existente) — atrapa §4.1.
2. `test_collab_router_http.py` con `TestClient` + SQLite para config/perfiles/permisos — atrapa §4.2, §5.5.
3. `build_transcript` con mensajes reales (placeholders/comandos filtrados).
4. Mover el hack de `importlib.metadata.version` (repetido en 16 ficheros) a `conftest.py`.

---

## 8. Valoración global

**Estado:** funcional y sorprendentemente robusto para su categoría, **en el escenario para el que está pensado** (local, un usuario, un worker). Fuera de ese escenario, la postura de seguridad es peligrosa por defecto y aparecen carreras de concurrencia reales.

**Principales puntos fuertes:** `engine.py` (persistencia con eventos, leases y CAS), escritura atómica de ficheros, aislamiento de fallos por agente, disciplina de ciclos de vida en el frontend, ausencia de XSS, y una suite de tests que —donde existe— es de integración real.

**Principales problemas:** (1) el bug de votación que rompe el consenso con alias activados; (2) un protocolo de decisión frágil basado en literales de texto sin anclar; (3) seguridad desactivada por defecto con un bypass confirmado de la whitelist; (4) ciclo de vida asíncrono con puntos que pueden romper la garantía de "una ronda por canal"; (5) complejidad concentrada en dos funciones-dios sin tests de comportamiento del consenso ni del router.

---

## 9. Plan de mejora por prioridad

### 🔴 Crítico (rompe el mecanismo central)
1. **Arreglar la votación** (§4.1): guardar y comparar por `agent_id`, no por nombre. **+ test** de `_vote_on_proposal` con override.

### 🟠 Alto (bugs de uso normal / seguridad seria / deuda que ya mordió)
2. **Anclar los marcadores de consenso** y parsear el último JSON del voto (§6.1).
3. **Blindar el ciclo de vida del lease** (§5.7): try/except + reintentos en renovación; garantizar `release_lease` en el finally.
4. **Mover la I/O de disco fuera del event loop** (§5.8): `snapshot`/árbol en `to_thread`.
5. **Revalidar `project_dir` en el camino de perfiles** (§4.2) y validar `config` importado.
6. **Conectar la validación de overrides** o eliminarla (§4.3).
7. **Frontend:** que el cliente API lance `{status, detail}` y detectar 409 por status (§4.7); flag `dirty` en overrides (§4.8); `try/catch` + estado de error en el panel (§4.9).
8. **Seguridad de despliegue** (§5.1–5.5): exigir `COLLAB_ALLOWED_ROOTS`/modo local para `/browse`, `project_dir` y `open-vscode`; exigir gestión explícita para operaciones sensibles en canales públicos.
9. **Tests HTTP del router** (§7): `TestClient` para permisos/409/perfiles.

### 🟡 Medio (casos límite / deuda próxima)
10. Techo de sanidad en el polling con `turn_timeout=0` (§5.9).
11. `return_exceptions=True` en los `gather` + aviso al canal cuando la ronda peta (§5.10, §5.17).
12. Semántica única de `context_messages` incl. 0 (§4.5); centralizar magic numbers (§6.6).
13. Conectar `escape_like` (§4.4); ocultar `recent_dirs` a solo-lectura (§4.6).
14. Limpiar placeholders huérfanos tras crash (§5.12); orden provider-first en backpressure (§5.11).
15. `resync` sin regresión (§5.14); paginación/snapshot de eventos (§5.15).
16. Implementar o eliminar el estado `incorporated` (§4.10); centralizar clasificación de errores (§6.5).

### 🟢 Bajo (pulido, código muerto, coherencia)
17. Borrar código muerto (`presets.py`, `_HARD_TURN_TIMEOUT`, `fallback_role`, `import json`).
18. TTL en estado en memoria (§6.7); boilerplate transaccional a `_session_scope`.
19. Corregir la documentación (líneas, nº de módulos, defaults de guardrails).
20. A11y, deduplicación de constantes, decisión explícita sobre i18n.

---

## 10. Propuesta de arquitectura alternativa

Solo tiene sentido si el módulo va a crecer o a soportar más de un worker/usuario. Para uso local puntual, basta con el plan §9.

**a) Separar "política" de "mecánica" con un runtime inyectado.** El problema raíz es que `orchestrator.py` es a la vez la máquina de estados de la ronda, el cliente de los modelos, el gestor de errores del proveedor y el bus de notificaciones. Tres capas:
- `engine.py` tal cual (la mejor pieza).
- `CollabRuntime`: encapsula lo que hoy son imports tardíos circulares — `complete(agent, system, prompt, opts) -> Result`, `notify(...)`, `emit_event(...)` — con la **clasificación de errores tipada dentro de `Result`**, no regex dispersos.
- Política pura: `RoundScheduler` (handraise/roundrobin/nudges como estrategias), `TurnExecutor` (prompt + retries) y `ConsensusService` (propuestas/votos por `agent_id`). Cada pieza se testea con un `FakeRuntime` de 30 líneas en vez de 17 monkeypatches, y voting/agents_status dejan de importar orchestrator.

**b) Una sola fuente de verdad de config.** Fusionar `channel.meta['collab']` y `collab_channel_config` en la tabla propia (que ya tiene versión): el motor lee la tabla, el panel escribe la tabla, `channel.meta` deja de participar (o se escribe como caché en la misma transacción). Elimina `sync_channel_config_from_meta`, `_sync_channel_meta`, `ensure_channel_config` y toda la clase de bugs "el panel dice X, el motor hace Y".

**c) Protocolo estructurado en vez de literales.** Para modelos con tool-calling, hacer de las tools el camino primario (`propose_finish`, `vote(agree, reason)`) y dejar los marcadores de texto como fallback **anclado** solo para pipes CLI. El consenso deja de depender del estilo de redacción de cada modelo — justo lo que no se controla en una mesa heterogénea de "IA gratuïtes/local".

**d) Decisión explícita sobre concurrencia.** O se declara single-worker (y se eliminan ~150 líneas de lease renewal + reconcile), o se mueve el estado de cancelación/stop a `collab_state` con polling en los puntos seguros. Pagar el coste del modelo durable sin su beneficio es la peor de las dos opciones.

---

## 11. Backlog accionable (tareas de desarrollo)

| ID | Tarea | Prioridad | Ficheros |
|---|---|---|---|
| MR-01 | Votación por `agent_id`; el proponente nunca vota | 🔴 | `voting.py`, `orchestrator.py`, `tasks.py` |
| MR-02 | Test `_vote_on_proposal` con override de nombre | 🔴 | `test/` (nuevo) |
| MR-03 | Anclar marcadores de consenso (`^FEINA_ACABADA:` MULTILINE, últimas líneas) | 🟠 | `orchestrator.py` |
| MR-04 | Voto: parsear último bloque JSON válido | 🟠 | `voting.py` |
| MR-05 | Lease: try/except + reintentos en renovación; `release_lease` garantizado | 🟠 | `orchestrator.py` |
| MR-06 | `snapshot`/árbol de ficheros vía `asyncio.to_thread` | 🟠 | `orchestrator.py`, `context.py` |
| MR-07 | Revalidar `project_dir` en `apply_profile`/`import_profile`/`update_profile` | 🟠 | `profiles.py` |
| MR-08 | Conectar `_validate_overrides` + tipar `ProfileForm.agent_overrides` | 🟠 | `profiles.py` |
| MR-09 | Cliente API lanza `{status, detail}`; 409 por status | 🟠 | `src/lib/apis/collab/index.ts`, `CollabPanel.svelte` |
| MR-10 | Flag `dirty` en overrides (no descartar ediciones) | 🟠 | `CollabProfiles.svelte` |
| MR-11 | `try/catch`+toast en acciones; rama de error en el panel | 🟠 | `CollabProfiles.svelte`, `CollabPanel.svelte` |
| MR-12 | Exigir `COLLAB_ALLOWED_ROOTS`/modo local para `/browse`, `project_dir`, `open-vscode` | 🟠 | `router.py`, `config.py` |
| MR-13 | Gestión explícita (owner/admin) para operaciones sensibles en canales públicos | 🟠 | `router.py` |
| MR-14 | `test_collab_router_http.py` (permisos, 409, perfiles) | 🟠 | `test/` (nuevo) |
| MR-15 | Techo de sanidad en polling con `turn_timeout=0` | 🟡 | `orchestrator.py`, `turns.py` |
| MR-16 | `return_exceptions=True` en gather + aviso al canal si la ronda peta | 🟡 | `orchestrator.py`, `voting.py` |
| MR-17 | Semántica única de `context_messages` (helper, incl. 0) | 🟡 | `context.py`, `config.py` |
| MR-18 | Conectar `escape_like` en `search_conversation` | 🟡 | `history.py` |
| MR-19 | Ocultar `recent_dirs` a usuarios de solo lectura | 🟡 | `router.py`, `config.py` |
| MR-20 | Limpiar placeholders "treballant" huérfanos al arrancar | 🟡 | `orchestrator.py` |
| MR-21 | Backpressure provider-first; no reconstruir en caliente | 🟡 | `backpressure.py` |
| MR-22 | `resync` sin regresión (`latestUserSeq >= currentSeq`) | 🟡 | `CollabAgentsBar.svelte` |
| MR-23 | Paginación/snapshot de eventos + poda de `rounds` | 🟡 | `router.py`, `engine.py`, `CollabAgentsBar.svelte` |
| MR-24 | Bloquear escritura en `.git/`/`IGNORED_DIRS`; whitelist de extensiones | 🟡 | `files.py` |
| MR-25 | Centralizar clasificación de errores en `usage.classify_error` + test de contrato | 🟡 | `usage.py`, `orchestrator.py`, `agents_status.py` |
| MR-26 | Implementar o eliminar estado `incorporated` | 🟡 | `engine.py`, `orchestrator.py` |
| MR-27 | Borrar código muerto (`presets.py`, `_HARD_TURN_TIMEOUT`, `fallback_role`, `import json`) | 🟢 | varios |
| MR-28 | Corregir docs (líneas orchestrator, nº módulos, defaults guardrails) | 🟢 | `docs/ESTAT-ACTUAL.md`, `docs/collab-workspace.md` |
| MR-29 | A11y (aria-label, aria-live, Esc) + deduplicación de constantes de estado | 🟢 | `src/lib/components/collab/*` |
| MR-30 | (Estructural, opcional) Extraer `TurnExecutor`/`RoundScheduler`/`CollabRuntime` | 🟠/proyecto | `orchestrator.py` → módulos nuevos |
| MR-31 | (Estructural, opcional) Fuente única de config (una tabla) | 🟠/proyecto | `profiles.py`, `config.py`, `router.py`, `orchestrator.py` |

---

*El análisis original se realizó sin modificar código; posteriormente se implementó el plan de mejora (ver §0), con la suite en **260 tests en verde** (`260 passed` con `WEBUI_SECRET_KEY` y `PYTHONPATH=backend`) y sin errores nuevos de `svelte-check` en los ficheros collab. Los hallazgos "ERROR CONFIRMADO" se verificaron leyendo el código fuente. Los riesgos de seguridad marcados como "crítico en producción" eran aceptables bajo el supuesto de despliegue local mono-usuario; tras la Fase 2 (MR-12/13/24) el código además exige `COLLAB_ALLOWED_ROOTS` o modo local explícito para las operaciones ligadas al host.*
