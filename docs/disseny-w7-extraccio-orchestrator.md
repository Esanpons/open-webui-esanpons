# W7 — Disseny d'extracció d'`orchestrator.py` en mòduls

> **Objectiu:** Reduir `orchestrator.py` (~900 línies) en mòduls coherents mantenint
> signatures públiques i la suite verda després de cada pas.
>
> **Principi rector:** cada extracció ha de ser purament moure codi + re-importar.
> Zero canvis de lògica. Si cal tocar lògica, és un pas separat amb tests nous.

---

## Anàlisi de l'arxiu actual

`orchestrator.py` té aquests grups lògics identificables:

| Grup | Funcions/classes | Línies aprox. |
|---|---|---|
| Constants i estat global | `_active_rounds`, `_turn_cancellables`, regexes, `_PHILOSOPHY`, `_budget_notices`, `_handraise_failures` | ~50 |
| Integració circuit breaker | `_circuit_allows`, `_record_circuit_result` | ~25 |
| Integració pressupost | `_budget_model_or_none` | ~20 |
| Resolució d'agent + config | `_resolved_agent`, `_channel_budget`, `_effective_collab_config` | ~45 |
| Gestió de torns | `active_turn_id`, `cancel_turn`, `lock_turn_tool`, `unlock_turn_tool`, `_effective_turn_timeout`, `_mark_cancelled_message` | ~70 |
| Prompts | `_apply_agent_prompt`, `_phase_block`, `_PHILOSOPHY`, `_model_supports_effort` | ~80 |
| Context (transcript, board, tree) | `build_transcript`, `_participants_line`, `_project_block`, `_collab_ctx`, `_board_text` | ~60 |
| Events i missatges | `_emit_collab_event`, `_transition_receipt`, `_persist_user_message`, `_latest_user_event_seq`, `post_notice` | ~60 |
| Status d'agents (down/up) | `_mark_agent_down`, `_mark_agent_up` | ~25 |
| Quick completion | `_quick_completion` | ~55 |
| Hand-raising | `_handraise_one`, `handraise` | ~100 |
| Votació i resum | `_vote_on_proposal`, `_update_summary` | ~60 |
| Agent turn | `agent_turn`, `_run_generation_until_done`, `_run_with_backpressure` | ~130 |
| Round execution | `run_round`, `_renew_round_lease`, `reconcile_channel`, `is_round_active`, `request_stop`, `_next_agent` | ~140 |

---

## Pla d'extracció incremental (5 passos)

### Pas 1 — `collab/turns.py`: gestió de torns i cancel·lació

**El que es mou:**
- Estat: `_turn_cancellables: dict`
- Constant: `_HARD_TURN_TIMEOUT`
- Funcions: `active_turn_id()`, `cancel_turn()`, `lock_turn_tool()`, `unlock_turn_tool()`,
  `_effective_turn_timeout()`, `_mark_cancelled_message()`

**Signatures públiques (no canvien):**
```python
# collab/turns.py
_HARD_TURN_TIMEOUT: int = 600
_turn_cancellables: dict[str, dict] = {}

def active_turn_id(channel_id: str) -> str | None
async def cancel_turn(channel_id: str, turn_id: str | None = None, reason: str = "user_requested") -> bool
def lock_turn_tool(turn_id: str, tool: str) -> bool
def unlock_turn_tool(turn_id: str) -> bool
def _effective_turn_timeout(config: CollabConfig) -> int
async def _mark_cancelled_message(message_id: str, reason: str) -> None
```

**Dependències que necessita:**
- `from open_webui.collab.config import CollabConfig`
- `from open_webui.models.messages import MessageForm, Messages` (per `_mark_cancelled_message`)

**Al orchestrator.py després del pas:**
```python
from open_webui.collab.turns import (
    _turn_cancellables,
    _HARD_TURN_TIMEOUT,
    active_turn_id,
    cancel_turn,
    lock_turn_tool,
    unlock_turn_tool,
    _effective_turn_timeout,
    _mark_cancelled_message,
)
```

**Tests afectats:** Els tests de `test_collab_orchestrator_events.py` que fan
`monkeypatch.setattr(orchestrator, "cancel_turn", ...)` seguiran funcionant perquè
el re-export el deixa accessible com a `orchestrator.cancel_turn`.

**Risc:** Molt baix — codi sense dependències externes (excepte `Messages` i `MessageForm`).

---

### Pas 2 — `collab/prompts.py`: construcció de prompts i filosofia

**El que es mou:**
- Constant: `_PHILOSOPHY`
- Funcions: `_phase_block()`, `_apply_agent_prompt()`, `_model_supports_effort()`

**Signatures públiques:**
```python
# collab/prompts.py
PHILOSOPHY: str = "..."
SYSTEM_AUTHOR: dict = {"model_id": "collab:system", "model_name": "🤝 Taula rodona"}

def _phase_block(phase: str) -> str
def _apply_agent_prompt(system: str, resolved: dict, name: str) -> str
def _model_supports_effort(model: dict) -> bool
```

**Dependències:** Cap (codi pur).

**Risc:** Mínim — codi pur sense efectes secundaris.

---

### Pas 3 — `collab/context.py`: context del canal (transcript, board, projecte)

**El que es mou:**
- Funcions: `build_transcript()`, `_participants_line()`, `_project_block()`,
  `_collab_ctx()`, `_board_text()`

**Signatures públiques:**
```python
# collab/context.py
async def build_transcript(channel_id: str, config: CollabConfig, models: dict) -> str
def _participants_line(config: CollabConfig, models: dict, exclude: Optional[str] = None) -> str
def _project_block(config: CollabConfig, include_tree: bool = False) -> str
def _collab_ctx(channel: ChannelModel, config: CollabConfig) -> dict
async def _board_text(channel_id: str) -> str
```

**Dependències:**
- `from open_webui.collab.config import CollabConfig`
- `from open_webui.collab.files import tree_as_text`
- `from open_webui.collab.tasks import get_summary, get_tasks, get_down_agents, tasks_as_text`
- `from open_webui.models.messages import Messages`
- `from open_webui.models.users import Users`
- `from open_webui.utils.channels import replace_mentions`
- `from open_webui.models.channels import ChannelModel`

**Risc:** Baix. Les funcions ja tenen interfícies netes.

---

### Pas 4 — `collab/agents_status.py`: tracking d'agents caiguts/recuperats

**El que es mou:**
- Constants: `_ERROR_CONTENT_RE`, `_RETRY_DOWN_SECONDS`
- Funcions: `_mark_agent_down()`, `_mark_agent_up()`

**Signatures públiques:**
```python
# collab/agents_status.py
_ERROR_CONTENT_RE: re.Pattern
_RETRY_DOWN_SECONDS: int = 300

async def _mark_agent_down(request, channel, user, models: dict, agent_id: str, reason: str) -> None
async def _mark_agent_up(request, channel, user, models: dict, agent_id: str) -> None
```

**Dependències:**
- `from open_webui.collab.tasks import set_down_agent, clear_down_agent, get_down_agents`
- `post_notice` es passa com a paràmetre o es importa (veure nota).

**Decisió de disseny:** `post_notice` queda a `orchestrator.py` (perquè depèn de
`channels.new_message_handler` que és un import dins de funció per evitar cicles).
`agents_status.py` l'importa tardà:
```python
async def _mark_agent_down(...):
    from open_webui.collab.orchestrator import post_notice
    ...
```
O alternativament, `post_notice` es mou a un `collab/notices.py` independent
(pas opcional). Recomanat: import tardà per evitar el cicle.

**Risc:** Baix-moderat (el import tardà de `post_notice` pot ser fràgil si
l'estructura canvia). Verificar amb la suite completa després de l'extracció.

---

### Pas 5 — `collab/voting.py`: votació de consens i resum

**El que es mou:**
- Funcions: `_vote_on_proposal()`, `_update_summary()`

**Signatures públiques:**
```python
# collab/voting.py
async def _vote_on_proposal(request, channel, config: CollabConfig, user, models: dict, proposal: dict) -> tuple[bool, int, int]
async def _update_summary(request, channel, config: CollabConfig, user, models: dict) -> None
```

**Dependències:**
- `from open_webui.collab.config import CollabConfig`
- `from open_webui.collab.context import build_transcript, _board_text, _project_block` (després del pas 3)
- `from open_webui.collab.prompts import PHILOSOPHY` (després del pas 2)
- `from open_webui.collab.tasks import get_summary, set_summary, get_down_agents`

**Risc:** Baix.

---

## Resultat esperat

Després dels 5 passos, `orchestrator.py` contindria (~400 línies):

- Constants: `_active_rounds`, regexes de marcadors (`_FINISH_MARKER_RE`, etc.),
  `_budget_notices`, `_handraise_failures`
- Integració: `_circuit_allows()`, `_record_circuit_result()`, `_budget_model_or_none()`
- Resolució: `_resolved_agent()`, `_channel_budget()`, `_effective_collab_config()`
- Events: `_emit_collab_event()`, `_transition_receipt()`, `_persist_user_message()`,
  `_latest_user_event_seq()`, `post_notice()`
- Quick completion: `_quick_completion()`
- Hand-raising: `_handraise_one()`, `handraise()`
- Agent turn: `agent_turn()`, `_run_generation_until_done()`
- Round: `run_round()`, `_renew_round_lease()`, `reconcile_channel()`,
  `is_round_active()`, `request_stop()`, `_next_agent()`
- Entry point: `handle_collab_message()`

Això és una reducció de ~55% de línies i molt més llegible.

---

## Riscos i mitigacions

1. **Tests amb `monkeypatch.setattr(orchestrator, "func", ...)`** — Després de
   l'extracció, les funcions moure són accessibles via re-import. Però els tests
   que fan `monkeypatch.setattr` sobre el mòdul original **deixen de funcionar**
   perquè `orchestrator.py` només les re-importa; el codi que les crida ho fa
   des del mòdul destí.

   **Mitigació:** Per cada pas, actualitzar els tests afectats per fer
   `monkeypatch.setattr` sobre el nou mòdul. Per exemple:
   ```python
   # Abans:
   monkeypatch.setattr(orchestrator, "_phase_block", fake)
   # Després:
   monkeypatch.setattr(orchestrator, "_phase_block", fake)  # encara funciona via re-import
   ```

   **Important:** Els re-imports amb `from x import f` fan que `orchestrator.f`
   sigui una referència. Si un test fa `monkeypatch.setattr(orchestrator, "f", mock)`,
   canvia la referència al mòdul `orchestrator`, però el codi intern que crida `f()`
   ho fa des del mòdul destí (no des de `orchestrator`). Per tant:
   - **Si l'orchestrator crida `f()` directament** (com a funció local), el patch
     sí funciona.
   - **Si un test patcheja una funció que ja no es crida internament a orchestrator**
     (perquè s'ha mogut), el patch no té efecte.

   **Recomanació:** Mantenir tots els punts de crida dins d'orchestrator.py.
   És a dir, `_phase_block` es crida des de `agent_turn` i `_handraise_one`, que
   es queden a orchestrator.py. Si s'importa via `from prompts import _phase_block`,
   el patch sobre `orchestrator._phase_block` funciona perquè és el mateix
   objecte.

   Però atenció: `from prompts import _phase_block` fa que `orchestrator._phase_block`
   i `prompts._phase_block` siguin el **mateix objecte**. Quan es fa
   `monkeypatch.setattr(orchestrator, "_phase_block", mock)`, es reemplaça
   `orchestrator._phase_block` amb `mock`, però `prompts._phase_block` segueix sent
   l'original. Com que `agent_turn` (que està a orchestrator.py) crida
   `orchestrator._phase_block`, el patch funciona. ✅

   **Però** si `build_transcript` es mou a `context.py` i és cridada des de
   `handraise` (a orchestrator.py) via `from context import build_transcript`,
   el patch `monkeypatch.setattr(orchestrator, "build_transcript", mock)`
   funciona perquè `handraise` està a orchestrator.py i crida la referència local.

   **Conclusió:** mentre els punts de crida (funcions que usen les funcions mogudes)
   es quedin a orchestrator.py, els re-imports fan que els patches funcionin.

2. **Imports circulars** — Evitar que un mòdul extret importi `orchestrator.py`.
   Usar imports tardans dins de funcions quan calgui (com ja es fa amb `post_notice`).

3. **Compatibilitat amb `monkeypatch`** — Comprovat: els re-imports mantenen
   els patches funcionals mentre el codi que crida estigui a orchestrator.py.

---

## Criteris d'acceptació per pas

Després de CADA pas:
1. `pytest test/test_collab_*.py -x` — tots els tests passen.
2. `grep -r "from open_webui.collab.orchestrator import" backend/` — cap import
   trencat (les façanes segueixen exportant tot).
3. `git diff --stat` — només canvis de moviment + imports, zero canvis de lògica.

Després de TOTS els passos:
4. `npm run build` — build de producció verd (no afecta frontend, però per seguretat).
5. Suite completa de tests col·laboratius (189+ tests amb els nous T8–T10/CB1–CB5/BP1–BP2/D1–D2).
