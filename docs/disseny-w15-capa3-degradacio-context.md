# Disseny W15 Capa 3 — Degradació de context

> Autor: Z.ai.glm-5.2 · 18/07/2026
> Estat: disseny complet, pendent de revisió d'equip.
> Depèn de: W15 Capa 1 (telemetria) ✅, W15 Capa 2 (`budget.py` + integració) ✅.
> Relacionat: `docs/disseny-w15-capa2-pressupostos.md` §2 (estat DEGRADAT).

---

## Resum

W15 Capa 2 implementa `check_budget()` que retorna `BudgetDecision(degraded=True)` quan el consum supera el 80% del pressupost. Avui aquest camp `degraded` **es calcula però no s'usa** — l'orquestrador no redueix el context en estat degradat.

W15 Capa 3 implementa la reducció automàtica de context quan `degraded=True`, per estalviar tokens abans d'arribar al 100%.

| Component | Què fa | Esforç |
|---|---|---|
| `_context_reduction()` | Decideix quant context enviar segons l'estat del pressupost | S |
| `build_transcript()` adaptativa | Redueix el nombre de missatges en estat degradat | S |
| `_project_block()` adaptatiu | Suprimeix l'arbre de fitxers als hand-raises en estat degradat | S |
| `_handraise_one()` lleuger | Prompt de mà alçada més curt quan degradat | S |
| UI | Xip discret indicant "mode estalvi" | S |

**Total: M** — tot són canvis petits i localitzats a `orchestrator.py` (territori de Codex).

---

## 1. Decisió de reducció

La funció `_budget_model_or_none()` ja existeix i retorna el `BudgetDecision`. Ara també necessitem saber si estem en estat degradat per reduir el context, independentment de si la crida s'ha permès.

### Nou helper: `_is_degraded()`

```python
async def _is_degraded(channel_id: str) -> bool:
    """Retorna True si el pressupost està en estat degradat (>80%)."""
    budget = await _channel_budget(channel_id)
    if not budget:
        return False
    decision = await check_budget(channel_id, "_degradation_check", "any", budget)
    return decision.degraded
```

**Optimització:** per evitar cridar `check_budget` dues vegades (una a `_budget_model_or_none` i una altra a `_is_degraded`), es pot cachear el resultat per canal-ronda:

```python
# A nivell de run_round
degraded = await _is_degraded(channel.id)  # comprova una vegada per iteració

# Es passa com a paràmetre a handraise(), _handraise_one(), agent_turn()
```

---

## 2. Transcript adaptativa

`build_transcript()` actualment carrega `context_messages` (default 30). En estat degradat:

```python
async def build_transcript(channel_id, config, models, *, degraded=False):
    limit = int(config.guardrail("context_messages") or 30)
    if degraded:
        limit = min(limit, 5)  # mai més de 5 missatges en degradació
    messages = (await Messages.get_messages_by_channel_id(channel_id, 0, limit))[::-1]
    # ... resta sense canvis
```

**Estalvi estimat:** 5 missatges en lloc de 30 redueix el context en ~83% dels tokens d'input per als hand-raises.

---

## 3. Arbre de fitxers suprimit en hand-raise

Avui `_project_block(config, include_tree=True)` només es crida a `agent_turn()`. Però `_handraise_one()` crida `_project_block(config)` (sense arbre) — això ja és correcte.

En estat degradat, **també suprimim l'arbre del torn d'agent** (no del hand-raise, que ja no el té):

```python
# A agent_turn()
include_tree = config.project_dir and not degraded
system += _project_block(config, include_tree=include_tree)
```

**Estalvi estimat:** l'arbre de fitxers pot ser de 2-5K tokens segons la mida del projecte.

---

## 4. Hand-raise lleuger

Quan degradat, el prompt de mà alçada es redueix:

```python
if degraded:
    prompt = (
        f"Missatge recent: {transcript[-500:]}\n\n"
        "Vols intervenir? Respon NOMÉS: {\"intervene\": true|false, \"priority\": 1-5}"
    )
else:
    # prompt complet actual
```

---

## 5. Compensació amb resum

Si `auto_summary` està activat i hi ha un resum disponible, en estat degradat el resum **s'inclou sempre** al context del hand-raise per compensar la pèrdua de missatges:

```python
if degraded and summary:
    prompt = f"Resum de la feina:\n{summary}\n\n" + prompt
```

Així l'agent té el context essencial encara que no vegi tota la transcripció.

---

## 6. Implementació al codi

Tots els canvis són a `orchestrator.py` (territori de Codex). Aquí es detalla on intervenen:

### 6.1 `run_round()`

```python
while True:
    # ... config, models, etc.

    degraded = await _is_degraded(channel.id)  # una comprovació per iteració

    # ... handraise o roundrobin, passant `degraded`

    final_content = await agent_turn(
        request, channel, config, user, models, speaker,
        nudge=nudge, degraded=degraded
    )
```

### 6.2 `handraise()`

```python
async def handraise(..., degraded: bool = False):
    transcript = await build_transcript(channel.id, config, models, degraded=degraded)
    # ... la resta igual
```

### 6.3 `_handraise_one()`

```python
async def _handraise_one(..., degraded: bool = False):
    # ... prompt lleuger si degradat
```

### 6.4 `agent_turn()`

```python
async def agent_turn(..., degraded: bool = False):
    transcript = await build_transcript(channel.id, config, models, degraded=degraded)
    # ... suprimir arbre si degradat
```

---

## 7. UI (frontend)

Un xip discret a la barra d'agents indica mode estalvi:

```svelte
{#if degraded}
  <span class="text-xs text-amber-400" title="Pressupost al {threshold}% — mode estalvi actiu">
    ⚡ Estalvi
  </span>
{/if}
```

El frontend pot rebre l'estat degradat de dues maneres:
1. **Endpoint REST:** `GET /{channel_id}/budget/status` retorna `{ "degraded": true, "threshold": 0.8 }`.
2. **Socket push:** `collab:budget_changed` emès quan l'estat canvia.

---

## 8. Criteris d'acceptació

1. Quan el pressupost està en estat degradat (>80%), el context es redueix automàticament:
   - `context_messages` limitat a 5
   - Arbre de fitxers suprimit als torns
   - Prompt de hand-raise més curt
   - Resum inclòs per compensar
2. La reducció és transparent: els agents encara reben prou context per respondre correctament.
3. La reducció es desactiva automàticament quan el pressupost baixa del threshold (p. ex. l'usuari puja el límit).
4. El xip "⚡ Estalvi" és visible a la UI quan el mode estalvi està actiu.
5. Un test verifica que `build_transcript(degraded=True)` retorna menys missatges que `build_transcript(degraded=False)`.

---

## 9. Riscos i mitigacions

| Risc | Mitigació |
|---|---|
| Reduir a 5 missatges pot perdre context crític | Incloure sempre el resum + l'últim missatge humà |
| `check_budget` es crida dues vegades (gate + degradació) | Cachejar `degraded` per iteració de ronda |
| Models locals (cost 0) mai entren en degradació | Correcte — la degradació només aplica si hi ha pressupost actiu |
| L'usuari no sap per què el context s'ha reduït | Xip visible + tooltip al frontend |
