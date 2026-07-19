# Disseny W5–W8 — Salut, UX, mantenibilitat i seguretat

> Autor: Z.ai.glm-5.2 · 18/07/2026
> Estat: disseny complet, pendent de revisió d'equip.
> Relacionat: `docs/auditoria-collab.md` §5.1 (línies W5–W8).

---

## Resum

| Bloc | Què resol | Estat actual | Esforç |
|---|---|---|---|
| **W5 — Salut i càrrega** | Circuit breaker persistent, backpressure global, validació de models | Telemetria completa (Capa 1) i classificació d'errors (6 categories). Sense circuit breaker ni backpressure. | M |
| **W6 — Qualitat UX** | Errors llegibles, push via socket, accessibilitat/teclat | Panell + barra + toast de conflicte + missatges bàsics. Sense normalització completa ni accessibilitat. | M |
| **W7 — Mantenibilitat** | Extraccions, i18n, checklist de cobertura | Mòduls separats i provats. Sense extracció d'`orchestrator.py` ni i18n complet. | S |
| **W8 — Seguretat menor** | Path traversal + escapament LIKE | **Implementat i testat** ✅ | S |

---

## W5 — Salut i càrrega

### Problema

Avui la salut d'un agent és heurística (`_ERROR_CONTENT_RE`, `_mark_agent_down`). El backend no té:
1. **Circuit breaker persistent:** un agent que ha fallat 3 vegades per `quota_exceeded` hauria de marcar-se com a `circuit_open` i no rebre més crides fins a un cooldown.
2. **Backpressure global:** si 10 canals activen rondes simultàniament, no hi ha cap límit — cada canal dispara les seves crides a l'API en paral·lel.
3. **Validació de models:** `config.agents` accepta qualsevol string com a `model_id` (S3).

### Arquitectura

#### 5.1 Circuit breaker persistent

Utilitzar `collab_state` (ja existeix, W4-3) amb claus estructurades:

```
key: "circuit:{agent_id}"
value: {
  "state": "closed" | "open" | "half_open",
  "consecutive_failures": int,
  "last_status": "quota_exceeded" | "provider_error" | ...,
  "opened_at": timestamp,
  "cooldown_seconds": 300
}
```

**Estat del circuit:**
- `closed` (normal): l'agent rep crides normalment.
- `open` (fallat): l'agent no rep crides. Es transita quan `consecutive_failures >= threshold` (default 3).
- `half_open` (prova): després de `cooldown_seconds`, una única crida de prova. Si passa → `closed`. Si falla → `open` + cooldown doblat.

**Integració (orquestrador, territori de Codex):**
- Abans de `agent_turn()` / `_handraise_one()`: comprovar `circuit:{agent_id}`. Si `open` i no ha passat el cooldown → l'agent es marca com a `down` amb motiu `circuit_open`.
- Després de cada crida: si `status != success` → incrementar `consecutive_failures`. Si `status == success` → reset a 0.

**No reintroduïu S1:** la lectura i escriptura del circuit es fa via `get_state_value` / `set_state_value` (upsert atòmic, sense read-modify-write sobre JSON).

#### 5.2 Backpressure global

Dos nivells:

1. **Semàfor global (asyncio.Semaphore):** límit de crides concurrents a APIs. Definit per `COLLAB_MAX_CONCURRENT_CALLS` (default 10). Cada `agent_turn()` i `_quick_completion()` l'adquireix.

```python
_global_semaphore = asyncio.Semaphore(COLLAB_MAX_CONCURRENT_CALLS)

async def agent_turn(...):
    async with _global_semaphore:
        # ... crida a l'API
```

2. **Semàfor per proveïdor:** si 8 agents usen OpenAI i 2 usen Ollama, el semàfor global permetria saturar OpenAI. Un semàfor addicional per prefix de model (extret de `model_id`) limita cada proveïdor.

**Configuració:** al `CollabConfig` o variable d'entorn. Valor per defecte conservador (10 global, 5 per proveïdor).

#### 5.3 Validació de models (S3)

Al `router.py`, endpoint `POST /{channel_id}/config`:

```python
if form_data.agents is not None:
    available = await get_all_models()  # ja existeix a Open WebUI
    invalid = [a for a in form_data.agents if a not in available]
    if invalid:
        raise HTTPException(400, f"Models no disponibles: {', '.join(invalid)}")
```

**No bloquejar canals existents:** si un model desapareix després de la configuració (p. ex. esborrat), l'orquestrador ja el marca com a `down`. La validació només s'aplica a la creació/edició de config, no a l'arrencada.

### Criteris d'acceptació

- Un agent amb 3 errors consecutius de `quota_exceeded` es marca `circuit_open` i no rep més crides durant 5 minuts.
- Després del cooldown, una crida de prova (`half_open`) determina si es recupera.
- 10 canals actius no disparen més de `MAX_CONCURRENT_CALLS` crides simultànies.
- `config.agents` rebutja `model_id`s que no existeixen als models disponibles.
- El circuit breaker es persisteix a `collab_state` (sobreviu a reinicis).

---

## W6 — Qualitat UX

### Problema

1. **Errors fràgils (F5):** `[object Object]`, status perdut, silencis.
2. **Push via socket (F3):** el panell fa polling cada 7s; la barra d'agents ja té push però el panell de config/tasques no.
3. **Accessibilitat (F6):** botons només-emoji, modal sense focus-trap, sense navegació per teclat.

### Arquitectura

#### 6.1 Normalització d'errors

Contracte d'error unificat per a totes les respostes del router collab:

```typescript
interface CollabError {
  status: number;       // HTTP status
  code: string;         // "config_conflict" | "quota_exceeded" | "model_not_found" | ...
  message: string;      // missatge humà en català
  detail?: unknown;     // informació tècnica opcional
}
```

Al frontend (`collab/index.ts`), totes les crides a la API catchejen i converteixen a `CollabError`:

```typescript
function parseCollabError(e: unknown): CollabError {
  if (e?.response) {
    const { status, data } = e.response;
    return { status, code: data?.code ?? "unknown", message: data?.detail ?? "Error desconegut" };
  }
  return { status: 0, code: "network", message: "Error de xarxa" };
}
```

El `CollabPanel.svelte` mostra `error.message` amb un tipus d'alerta segons `error.code`:
- `config_conflict` → toast groc ⚠️
- `quota_exceeded` → toast vermell 🔴 amb botó "Veure consum"
- `model_not_found` → toast groc amb llista de models disponibles
- `network` → toast gris amb botó "Reintenta"

#### 6.2 Push via socket per config/tasques

**Estats que ja tenen push:** agent_status, receipts, events.
**Estats que fan polling:** config del canal, tasques, fitxers del projecte.

Implementar dos canals de socket nous:
- `collab:config_changed` — emès quan `save_collab_config` o `update_channel_config` modifiquen la config. El panell el rep i refresca.
- `collab:tasks_changed` — emès quan es crea/actualitza/esborra una tasca. El panell el rep i refresca.

**Patró:** el polling es manté com a fallback (cada 30s, no 7s), però el push és primari.

#### 6.3 Accessibilitat

- Tots els botons d'emoji tenen `aria-label` descriptiu (`aria-label="Atura l'equip"` per ⏹).
- El modal de config té `role="dialog"`, `aria-modal="true"`, focus-trap (Trap del primer al darrer element focusable, Esc per tancar).
- Navegació per teclat: Tab circula per botons → panell → barra; Enter activa el botó amb focus.
- `prefers-reduced-motion`: desactivar animacions de transició d'estats.

### Criteris d'acceptació

- Cap missatge d'error mostra `[object Object]` o text en anglès.
- El panell de config s'actualitza en temps real quan un altre procés la canvia (sense polling).
- El modal es pot obrir, navegar i tancar només amb teclat.
- Els botons tenen `aria-label` i són accessibles amb lector de pantalla.

---

## W7 — Mantenibilitat

### Problema

1. **`run_round` / `orchestrator.py` massa gran** (B9): 58KB, concentra scheduler, agent_turn, handraise, votació, resum, file_tools, error handling.
2. **Cadenes hardcoded en català** (F9): fora d'i18next.
3. **Cobertura de tests residual.**

### Arquitectura

#### 7.1 Extraccions d'`orchestrator.py`

Extreure en mòduls independents (cadascun amb els seus tests):

| Mòdul nou | Què extreu | Linies aprox. |
|---|---|---|
| `turn_runner.py` | `agent_turn()`, `_quick_completion()`, construcció de context, system prompt, form_data | ~400 |
| `speaker_policy.py` | `handraise()`, ordenació de voluntaris, `roundrobin`, prioritats | ~200 |
| `vote_collector.py` | `collect_votes()`, còmput de resultats, `propose_finish` | ~150 |
| `event_publisher.py` | Emissió socket de `agent_status`, `receipt_updated`, `collab:config_changed` | ~100 |

L'orquestrador es queda com a coordinador: adquireix lease, crida `turn_runner.run()`, publica events, gestiona el cicle continu.

**Risc:** aquesta extracció és la més delicada — toca `orchestrator.py`, que és territori de Codex. Cal fer-la en passes petites amb tests de regressió abans i després.

#### 7.2 i18n

Moure totes les cadenes en català hardcoded al sistema d'i18n d'Open WebUI:

```typescript
// Abans:
toast.error("Error desconegut");

// Després:
toast.error($i18n.t("Error desconegut"));
```

Fitxer de traduccions: `src/lib/i18n/locales/ca/translation.json` (o l'equivalent segons l'estructura del projecte).

#### 7.3 Checklist de cobertura

| Mòdul | Tests actuals | Cobertura |
|---|---|---|
| `engine.py` | 14+ | Bona |
| `orchestrator.py` (events) | 12+ | Bona |
| `tasks.py` | 2+ | Mínima |
| `files.py` | 4+4 nous (W8) | Bona |
| `config.py` | 4+ (versioning) | Bona |
| `budget.py` | 10+ | Bona |
| `profiles.py` | 9+ | Bona |
| `usage.py` | ✓ | Bona |
| `turn_runner.py` (nou) | 0 | Pendent |
| `speaker_policy.py` (nou) | 0 | Pendent |
| `vote_collector.py` (nou) | 0 | Pendent |

### Criteris d'acceptació

- `orchestrator.py` baixa de ~1800 línies a <800 (coordinador pur).
- Cap cadena en català hardcoded (tot via `$i18n.t`).
- Cada mòdul extret té tests propis.
- Refactor no trenca cap test existent.

---

## W8 — Seguretat menor ✅ IMPLEMENTAT

### S4 — Path traversal (resolve_safe)

**Implementat:** `resolve_safe()` a `files.py` usa `Path.resolve()` + check de parents. Bloqueja:
- `..` que surt del projecte
- Rutes absolutes fora
- Symlinks que apunten fora
- Null bytes
- Separadors mixtes

**Tests nous:** `test/test_collab_security.py` amb 15 tests cobrint:
- Rutes vàlides (root, subpath, relatives amb `.`)
- Atacs (`..`, absolutes fora, symlinks escapant, null bytes)
- Edge cases (None, ruta inexistent, casing, separadors mixtes)

### S6 — Escapament LIKE

**Implementat:** `escape_like()` a `files.py`. Escapa `%`, `_` i `\\` amb backslash.

**Verificació:** no hi ha cap consulta LIKE al mòdul collab actualment — tot usa `=` exacte. La funció és preventiva per a futures consultes.

**Tests nous:** 4 tests a `test_collab_security.py` cobrint escapament bàsic, buit, backslash i múltiples ocurrències.

### Criteris d'acceptació — TOTS COMPLERTS ✅

- `resolve_safe` bloqueja tots els vectors de path traversal provats.
- `escape_like` escapa correctament `%`, `_` i `\\`.
- No hi ha consultes LIKE insegures al mòdul collab.
