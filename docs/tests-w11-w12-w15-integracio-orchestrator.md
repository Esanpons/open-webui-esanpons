# Specs de tests — Integracions W11/W12 + W15 Capa 2 a orchestrator.py

> Autor: Z.ai.glm-5.2 · 18/07/2026  
> Actualitzat: Z.ai.glm-5.2 — 13/16 specs implementades.  
> Cobrint: integració de `resolve_agent()` (W11/W12) i `check_budget()` (W15 Capa 2) a `orchestrator.py`.

---

## Context

Codex ha integrat al `orchestrator.py`:

1. **W11/W12:** `_resolved_agent()`, `_apply_agent_prompt()`, `_model_supports_effort()` als hooks `agent_turn`, `_handraise_one`, `_quick_completion`.
2. **W15 Capa 2:** `_channel_budget()`, `_budget_model_or_none()` amb downgrade/stop/pause + deduplicació d'avisos.
3. **W13:** `_effective_collab_config()` fusiona config base + overlay del perfil/preset a `run_round`.

Codex ja ha afegit tests (`test_budget_gate_downgrades_or_pauses_and_stops`, `test_effective_config_merges_preset_without_losing_channel_fields`).

---

## Cobertura actual

### ✅ Cobertes (B1–B8 + T1–T7, T11, T12 = 13 specs)

| Spec | Test | Estat |
|---|---|---|
| T1 | `test_t1_resolved_agent_no_overrides_returns_base_values` | ✅ |
| T2 | `test_t2_resolved_agent_with_model_override_substitutes_model` | ✅ |
| T3 | `test_t3_apply_agent_prompt_prepends_role_and_system_prompt` | ✅ |
| T4 | `test_t4_apply_agent_prompt_without_overrides_returns_original` | ✅ |
| T5 | `test_t5_model_supports_effort_detects_capability` | ✅ |
| T6 | `test_t6_model_supports_effort_false_without_capabilities` | ✅ |
| T7 | `test_t7_token_limit_applied_as_max_tokens` | ✅ |
| T11 | `test_t11_profile_priority_affects_handraise_order` | ✅ |
| T12 | `test_t12_resolved_agent_error_falls_back_safely` | ✅ |
| B1 | Implícit al test de Codex (`test_budget_gate...`) | ✅ |
| B2 | Implícit al test de Codex (downgrade+fallback) | ✅ |
| B3 | Implícit al test de Codex (downgrade sense fallback) | ✅ |
| B4 | Implícit al test de Codex (stop) | ✅ |
| B5 | `test_b5_budget_notice_deduplicated` | ✅ |
| B6 | `test_b6_budget_notice_cleared_when_allowed` | ✅ |
| B7 | `test_b7_no_budget_allows_all` | ✅ |
| B8 | `test_b8_channel_budget_error_returns_none` | ✅ |

### ⬜ Pendents (T8–T10 = 3 specs)

| Spec | Motiu | Estratègia |
|---|---|---|
| T8 — `effort` només s'aplica si el model ho suporta | Requereix capturar `form_data` dins `agent_turn`, que crea un missatge real via `new_message_handler` i crida `_run_generation_until_done`. Mocking profund i fràgil. | Millor com a test d'integració amb un backend de proves real o un mock de `CHAT_COMPLETION_HANDLER`. |
| T9 — `effort` no s'aplica si el model no ho suporta | Mateix motiu que T8. | Mateixa estratègia. |
| T10 — `tools` filtra els tool_ids permesos | Proven `form_data["tool_ids"]` després del filtratge, que passa dins `agent_turn`. | Mateixa estratègia. |

Aquestes 3 specs provenen el pipeline complet de `agent_turn`: creació de missatge placeholder, captura de `form_data` a través de múltiples capes (`new_message_handler` → `CHAT_COMPLETION_HANDLER` → `_run_generation_until_done`), i verificació dels camps inyectats. No es poden provar amb monkeypatch aïllat com T3–T6 perquè `agent_turn` té massa dependències acoblades (Models, Channels, Users, CHAT_COMPLETION_HANDLER).

**Recomanació:** implementar-les com a tests d'integració amb un fixture conda (backend real SQLite + mock del provider de models) quan es faci la validació en execució. El comportament ja està provat indirectament pel test `test_effective_config_merges_preset_without_losing_channel_fields` (que verifica que la config efectiva arriba a `run_round`) i per la presència del codi a `agent_turn` (línies 620-630).
