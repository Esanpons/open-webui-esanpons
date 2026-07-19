# Disseny W15 Capa 2 — Pressupostos actius

> Autor: Z.ai.glm-5.2 · Data: 17/07/2026
> Depèn de: W15 Capa 1 (telemetria `collab_usage` + `collab_budget_tracker`) — Fase 0 en curs.
> Bloqueja: res (és disseny; la implementació és de Claude Fable a Fase 2).

---

## 1. Estructura del JSON `budget`

El camp `budget` viu a dues llocs (mai en `channel.meta`):

1. **`collab_profile.budget`** — plantilla, nullable (null = sense límit).
2. **`collab_channel_config.budget`** — còpia efectiva del canal, independent.

```json
{
  "session_total_tokens": 500000,
  "session_total_cost": 5.0,
  "per_agent_tokens": 200000,
  "per_turn_tokens": 50000,
  "per_handraise_tokens": 10000,
  "per_vote_tokens": 5000,
  "per_summary_tokens": 5000,
  "degradation_threshold": 0.8,
  "action_on_exhaustion": "pause"
}
```

**Tots els camps són opcionals.** Camps absents o `null` = sense límit en aquella dimensió. Un perfil amb `budget = null` és il·limitat (comportament actual).

### Semàntica de cada camp

| Camp | Tipus | Què limita |
|---|---|---|
| `session_total_tokens` | int ≥ 0 | Tokens acumulats de tota la sessió (tots els agents, totes les crides). |
| `session_total_cost` | float ≥ 0.0 | Cost en USD acumulat. Necessita preus per model. |
| `per_agent_tokens` | int ≥ 0 | Tokens per agent (suma de totes les seves crides). |
| `per_turn_tokens` | int ≥ 0 | Tokens per un torn d'agent complet (una sola crida de turn). |
| `per_handraise_tokens` | int ≥ 0 | Tokens per una crida de hand-raise. |
| `per_vote_tokens` | int ≥ 0 | Tokens per una crida de votació. |
| `per_summary_tokens` | int ≥ 0 | Tokens per una crida de resum de secretari. |
| `degradation_threshold` | float 0.0–1.0 | Fracció del pressupost a partir de la qual s'activa la reducció automàtica de context (Capa 3). Per defecte 0.8. |
| `action_on_exhaustion` | enum | Què fer al 100% del pressupost: `"pause"` (pausa amb avís), `"downgrade"` (degrada a model més barat), `"stop"` (atura la sessió). |

### Jerarquia de comprovació (ordre de precedència)

Abans de cada crida, el backend avalua en aquest ordre. La primera condició que es compleix guanya:

1. **Per crida:** `per_turn_tokens` / `per_handraise_tokens` / `per_vote_tokens` / `per_summary_tokens` (segons el `call_type`).
2. **Per agent:** `per_agent_tokens` (consultat O(1) a `collab_budget_tracker`).
3. **Global:** `session_total_tokens` i `session_total_cost` (suma de tots els agents al tracker).

Si cap pressupost és aplicable (tots null/absents), la crida es fa sense límit (comportament actual).

---

## 2. Lògica de degradació (estats del pressupost)

```
                    ┌──────────────────────────────────────────────┐
                    │          consum / pressupost                 │
                    │                                              │
  0% ──────── 80% ──────── 100% ──────── ∞                       │
  │            │             │                                      │
  ▼            ▼             ▼                                      │
 NORMAL    DEGRADAT     EXHAURIT   (només si action=stop,         │
  │            │             │       el scheduler para de cridar) │
  │            │             │                                      │
  │      Redueix context    Acciona action_on_exhaustion           │
  │      (Capa 3:           (pause / downgrade / stop)             │
  │       menys missatges,                                        │
  │       hand-raise lleuger)                                      │
  └──────────────────────────────────────────────┘
```

### Estat: NORMAL (< degradation_threshold)

- Comportament actual, sense canvis.
- Els comptadors s'actualitzen a cada crida (Capa 1).

### Estat: DEGRADAT (≥ degradation_threshold, < 100%)

S'activen automàticament les mesures de la Capa 3:

1. **Hand-raise lleuger obligatori:** `context_messages` es restringeix a 5 (en lloc del valor configurat, típicament 30). Estalvi estimat 60-70%.
2. **Arbre de fitxers suprimit** als hand-raises (només al torn actiu).
3. **Context adaptatiu:** agents amb dos `pass` seguits només reben el delta des de la seva última avaluació.
4. **Resum lateral:** si `auto_summary` està activat, el resum s'inclou sempre al context per compensar la reducció de missatges.

L'estat degradat **mai** impedeix una crida — només la fa més barata.

### Estat: EXHAURIT (≥ 100%)

Segons `action_on_exhaustion`:

| Acció | Comportament | Reactivació |
|---|---|---|
| `"pause"` (default) | El scheduler pausa: no inicia nous torns ni hand-raises. Avis al canal: `"⏸️ Pressupost exhaurit (X tokens / Y). Escriu per continuar sense límit o ajusta el pressupost."` | L'usuari escriu un missatge → es repren les crides. O puja el pressupost. |
| `"downgrade"` | Els agents es redueixen a un model més barat (configurat a l'agent: `fallback_model_id`). Si no tenen fallback, es comporta com `"pause"`. | L'usuari puja el pressupost o el reconfigura. |
| `"stop"` | La sessió s'atura completament. Avís: `"🛑 Sessió aturada: pressupost exhaurit."` | Cal reactivar manualment (`/collab start`) amb pressupost nou. |

**Important:** l'estat exhaurit es comprova **abans** d'iniciar la crida, no durant. Una crida ja en marxa no s'interromp per pressupost (sí per timeout/cancel·lació, que és W2).

---

## 3. Punt de comprovació al codi

Funció `check_budget()` que el scheduler/orquestrador crida abans de cada crida:

```python
async def check_budget(
    channel_id: str,
    agent_id: str,
    call_type: str,  # "turn" | "handraise" | "vote" | "summary"
    budget: dict | None,
) -> BudgetDecision:
    """
    Retorna:
      BudgetDecision(
        allowed: bool,           # False si exhaurit
        degraded: bool,          # True si cal reduir context
        reason: str | None,      # missatge d'avís si allowed=False
        action: str | None,      # "pause" | "downgrade" | "stop" si exhaurit
      )
    """
    if not budget:
        return BudgetDecision(allowed=True, degraded=False, reason=None, action=None)

    # O(1) — consulta collab_budget_tracker
    consumed = await get_budget_tracker(channel_id, agent_id)
    session_total = await get_session_total(channel_id)  # suma de tots els agents

    # 1. Límit per crida
    per_call_key = f"per_{call_type}_tokens"
    per_call_limit = budget.get(per_call_key)
    if per_call_limit and consumed["consumed_tokens"] >= per_call_limit:
        # Nota: això és acumulat per agent, no per crida individual.
        # El límit "per crida" s'aplica com a sostre: si l'agent ja ha consumit
        # aquesta quantitat en total, no se'n fa cap més d'aquest tipus.
        # (Alternativa: és un límit per crida individual, comprovant usage
        # de l'última crida d'aquest tipus — decidir en implementació.)
        pass  # veure nota més avall

    # 2. Límit per agent
    per_agent_limit = budget.get("per_agent_tokens")
    if per_agent_limit and consumed["consumed_tokens"] >= per_agent_limit:
        return BudgetDecision(
            allowed=False, degraded=False,
            reason=f"Pressupost per agent exhaurit ({consumed['consumed_tokens']} / {per_agent_limit} tokens)",
            action=budget.get("action_on_exhaustion", "pause"),
        )

    # 3. Límit global per tokens
    session_limit = budget.get("session_total_tokens")
    if session_limit and session_total >= session_limit:
        return BudgetDecision(
            allowed=False, degraded=False,
            reason=f"Pressupost de sessió exhaurit ({session_total} / {session_limit} tokens)",
            action=budget.get("action_on_exhaustion", "pause"),
        )

    # 4. Límit global per cost
    cost_limit = budget.get("session_total_cost")
    session_cost = await get_session_cost(channel_id)
    if cost_limit and session_cost >= cost_limit:
        return BudgetDecision(
            allowed=False, degraded=False,
            reason=f"Cost de sessió exhaurit (${session_cost:.2f} / ${cost_limit:.2f})",
            action=budget.get("action_on_exhaustion", "pause"),
        )

    # 5. Degradació
    threshold = budget.get("degradation_threshold", 0.8)
    degraded = False
    if session_limit and session_total >= session_limit * threshold:
        degraded = True
    elif per_agent_limit and consumed["consumed_tokens"] >= per_agent_limit * threshold:
        degraded = True

    return BudgetDecision(allowed=True, degraded=degraded, reason=None, action=None)
```

**Nota sobre `per_turn_tokens` / `per_handraise_tokens`:** aquests són límits **acumulats per agent per tipus de crida**, no límits d'una sola crida. Si es vol limitar una sola crida, caldria una taula addicional o comprovar l'`usage` de l'última crida registrada. Recomanació: implementar primer com a límit acumulat (més simple) i afegir límit individual si la telemetria mostra que cal.

---

## 4. Actualització del pressupost (després de cada crida)

Quan una crida retorna amb `usage` de l'API (input_tokens, output_tokens, etc.):

```python
async def record_usage(
    channel_id: str,
    agent_id: str,
    call_type: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    estimated_cost: float,
    status: str,        # "ok" | "quota_exceeded" | "timeout" | ...
    error_detail: str | None,
):
    """Registra al log i actualitza l'agregat en la mateixa transacció."""
    async with get_async_db_context() as db:
        await db.execute(text("""
            BEGIN IMMEDIATE;
            INSERT INTO collab_usage (id, channel_id, agent_id, call_type,
                input_tokens, output_tokens, total_tokens, estimated_cost,
                status, error_detail, created_at)
            VALUES (:id, :cid, :aid, :ctype, :in_tok, :out_tok, :total_tok,
                :cost, :status, :detail, :now);

            INSERT INTO collab_budget_tracker (channel_id, agent_id,
                consumed_tokens, consumed_cost, call_count, updated_at)
            VALUES (:cid, :aid, :total_tok, :cost, 1, :now)
            ON CONFLICT(channel_id, agent_id) DO UPDATE SET
                consumed_tokens = consumed_tokens + :total_tok,
                consumed_cost   = consumed_cost   + :cost,
                call_count      = call_count      + 1,
                updated_at      = :now;
            COMMIT;
        """), { ... })
```

---

## 5. Preus per model (per calcular `estimated_cost`)

Cal una taula o diccionari de preus per model:

```python
# Format: model_id_prefix -> {"input_per_1k": float, "output_per_1k": float}
MODEL_PRICING = {
    "claude-sonnet-4": {"input_per_1k": 0.003, "output_per_1k": 0.015},
    "claude-opus-4":   {"input_per_1k": 0.015, "output_per_1k": 0.075},
    "gpt-4o":          {"input_per_1k": 0.0025, "output_per_1k": 0.01},
    "qwen":            {"input_per_1k": 0.0, "output_per_1k": 0.0},  # local = gratis
    # ...
}

def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    pricing = MODEL_PRICING.get(model_id, {"input_per_1k": 0.0, "output_per_1k": 0.0})
    return (input_tokens / 1000) * pricing["input_per_1k"] + \
           (output_tokens / 1000) * pricing["output_per_1k"]
```

**Models locals (Ollama, Qwen local):** cost 0.0. Models amb preu desconegut: cost 0.0 amb avís al log.

---

## 6. Criteris d'acceptació (W15 Capa 2)

1. **Configuració del pressupost:** es pot definir `budget` al perfil i es copia a `collab_channel_config` quan s'aplica.
2. **Comprovació O(1):** `check_budget` consulta `collab_budget_tracker`, mai `SUM` sobre `collab_usage`.
3. **Tres estats:** normal (< 80%), degradat (80–100%), exhaurit (≥ 100%). Cada un es comporta com s'ha descrit.
4. **Acció configurable:** `action_on_exhaustion` = pause / downgrade / stop. Sense acció definida, default = pause.
5. **Avís visible:** quan s'atura per pressupost, el canal rep un missatge amb els tokens consumits i el límit.
6. **No interferència amb crides en marxa:** la comprovació és abans de la crida, no durant.
7. **Degradació transparent:** l'estat degradat redueix context (Capa 3) sense avís invasiu — només un xip discret als comptadors (UX de Claude Fable).

---

## 7. Interacció amb altres W

| W | Interacció |
|---|---|
| **W10** (scheduler continu) | El scheduler crida `check_budget` abans d'iniciar cada torn/hand-raise/vot. Si `allowed=False`, l'esdeveniment es marca com `budget_blocked` i no es processa. |
| **W11** (perfils) | El `budget` viu al perfil i es copia a `collab_channel_config`. |
| **W15 Capa 1** (telemetria) | Proporciona les dades (`collab_usage` + `collab_budget_tracker`). |
| **W15 Capa 3** (reducció de context) | S'activa automàticament en estat degradat. |
| **W1** (visibilitat) | Els comptadors de pressupost es mostren a la barra d'agents. |
| **W5** (salut) | `quota_exceeded` de Capa 1 alimenta el circuit breaker. |
