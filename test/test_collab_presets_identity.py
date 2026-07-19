"""Tests per W13 (presets de mode) i W14 (identitat visual).

Cobreix:
- W13: presets disponibles, estructura correcta, preset_to_profile_form, extract_mode_from_config.
- W14: contrast_ratio, has_good_contrast, fallback_color estable, fallback_avatar,
  resolve_agent_identity amb/fallbacks, resolve_channel_identities.
"""

import importlib.metadata

import pytest

_real_version = importlib.metadata.version
importlib.metadata.version = (
    lambda name: "0.0.0" if name == "open-webui" else _real_version(name)
)

from open_webui.collab.presets import (
    PRESETS,
    PresetDefinition,
    extract_guardrails_from_config,
    extract_mode_from_config,
    get_preset,
    list_presets,
    preset_to_profile_form,
)
from open_webui.collab.identity import (
    BG_DARK,
    DEFAULT_PALETTE,
    AgentIdentity,
    contrast_ratio,
    fallback_avatar,
    fallback_color,
    has_good_contrast,
    resolve_agent_identity,
    resolve_channel_identities,
)


# ---------------------------------------------------------------------------
# W13: Presets
# ---------------------------------------------------------------------------


class TestPresets:
    def test_list_presets_returns_all_four(self):
        presets = list_presets()
        assert len(presets) == 4
        keys = {p["key"] for p in presets}
        assert keys == {"debate", "standup", "code_review", "quick_help"}

    def test_each_preset_has_valid_mode_and_conversation_mode(self):
        for key, preset in PRESETS.items():
            assert preset.mode in ("handraise", "roundrobin"), f"{key}: mode invàlid"
            assert preset.conversation_mode in ("continuous", "rounds"), (
                f"{key}: conversation_mode invàlid"
            )

    def test_debate_preset_allows_self_reply_unlimited_turns(self):
        p = PRESETS["debate"]
        assert p.guardrails["max_agent_turns"] == 0  # il·limitat
        assert p.guardrails["allow_self_reply"] is True

    def test_standup_uses_roundrobin_rounds(self):
        p = PRESETS["standup"]
        assert p.mode == "roundrobin"
        assert p.conversation_mode == "rounds"

    def test_quick_help_has_limited_context(self):
        p = PRESETS["quick_help"]
        assert p.guardrails["context_messages"] == 10
        assert p.guardrails["max_agent_turns"] == 5

    def test_get_preset_returns_definition(self):
        p = get_preset("code_review")
        assert isinstance(p, PresetDefinition)
        assert p.name == "Revisió de codi"

    def test_get_preset_unknown_returns_none(self):
        assert get_preset("nonexistent") is None

    def test_preset_to_profile_form_has_config_with_mode(self):
        p = get_preset("debate")
        form = preset_to_profile_form(p, "Test")
        assert "config" in form
        assert form["config"]["mode"] == "handraise"
        assert form["config"]["conversation_mode"] == "continuous"
        assert form["is_template"] is True

    def test_preset_to_profile_form_has_guardrails(self):
        p = get_preset("standup")
        form = preset_to_profile_form(p)
        assert form["config"]["guardrails"]["max_agent_turns"] == 3

    def test_extract_mode_from_config_defaults(self):
        mode, conv = extract_mode_from_config({})
        assert mode == "handraise"
        assert conv == "continuous"

    def test_extract_mode_from_config_with_values(self):
        mode, conv = extract_mode_from_config({
            "mode": "roundrobin",
            "conversation_mode": "rounds",
        })
        assert mode == "roundrobin"
        assert conv == "rounds"

    def test_extract_mode_from_config_invalid_falls_back(self):
        mode, conv = extract_mode_from_config({"mode": "banana", "conversation_mode": "grape"})
        assert mode == "handraise"
        assert conv == "continuous"

    def test_extract_guardrails_from_config(self):
        g = extract_guardrails_from_config({"guardrails": {"max_agent_turns": 5}})
        assert g == {"max_agent_turns": 5}

    def test_extract_guardrails_empty(self):
        assert extract_guardrails_from_config({}) == {}


# ---------------------------------------------------------------------------
# W14: Contrast WCAG
# ---------------------------------------------------------------------------


class TestContrastWCAG:
    def test_white_on_black_has_max_contrast(self):
        ratio = contrast_ratio("#ffffff", "#000000")
        assert ratio == pytest.approx(21.0, abs=0.1)

    def test_same_color_has_minimum_contrast(self):
        ratio = contrast_ratio("#3b82f6", "#3b82f6")
        assert ratio == pytest.approx(1.0, abs=0.01)

    def test_palette_colors_pass_aa_on_dark(self):
        """Tots els colors de la paleta han de tenir contrast ≥ 4.5 sobre el fons fosc."""
        for color in DEFAULT_PALETTE:
            assert has_good_contrast(color, BG_DARK), (
                f"Color {color} no compleix WCAG AA sobre {BG_DARK}"
            )

    def test_low_contrast_color_fails(self):
        # Gris molt fosc sobre fons fosc → baix contrast
        assert has_good_contrast("#2a2a2a", BG_DARK) is False

    def test_invalid_hex_returns_false(self):
        assert has_good_contrast("not-a-color", BG_DARK) is False
        assert has_good_contrast("#xyz", BG_DARK) is False

    def test_short_hex_works(self):
        """Colors de 3 dígits (#rgb) s'accepten."""
        ratio = contrast_ratio("#fff", "#000")
        assert ratio == pytest.approx(21.0, abs=0.1)


# ---------------------------------------------------------------------------
# W14: Fallback d'identitat
# ---------------------------------------------------------------------------


class TestFallbackIdentity:
    def test_fallback_color_is_stable(self):
        """El mateix nom sempre retorna el mateix color."""
        c1 = fallback_color("Qwen")
        c2 = fallback_color("Qwen")
        assert c1 == c2

    def test_fallback_color_different_names_preferably_different(self):
        """Noms diferents haurien de tenir colors diferents (probabilístic)."""
        colors = {fallback_color(n) for n in [
            "Qwen", "Claude", "Codex", "Z.ai", "GPT-4", "Gemini", "Llama", "Mistral"
        ]}
        # Amb 8 noms i 8 colors, és molt probable que n'hi hagi més d'un
        assert len(colors) >= 2

    def test_fallback_color_is_in_palette(self):
        c = fallback_color("TestAgent")
        assert c in DEFAULT_PALETTE

    def test_fallback_avatar_returns_first_letter_uppercase(self):
        assert fallback_avatar("Qwen") == "Q"
        assert fallback_avatar("claude") == "C"

    def test_fallback_avatar_empty_returns_question(self):
        assert fallback_avatar("") == "?"
        assert fallback_avatar("   ") == "?"


# ---------------------------------------------------------------------------
# W14: resolve_agent_identity
# ---------------------------------------------------------------------------


class TestResolveAgentIdentity:
    def test_no_override_uses_fallbacks(self):
        ai = resolve_agent_identity("agent-1", "Qwen", [])
        assert ai.agent_id == "agent-1"
        assert ai.name == "Qwen"
        assert ai.color in DEFAULT_PALETTE
        assert ai.avatar == "Q"
        assert ai.role is None

    def test_override_provides_all_fields(self):
        overrides = [{
            "model_id": "agent-1",
            "color": "#f87171",
            "avatar": "🧪",
            "role": "Tester",
        }]
        ai = resolve_agent_identity("agent-1", "Qwen", overrides)
        assert ai.color == "#f87171"
        assert ai.avatar == "🧪"
        assert ai.role == "Tester"

    def test_override_with_low_contrast_color_falls_back(self):
        overrides = [{
            "model_id": "agent-1",
            "color": "#222222",  # molt proper al fons fosc
        }]
        ai = resolve_agent_identity("agent-1", "Qwen", overrides)
        # No hauria d'usar el color de baix contrast
        assert ai.color != "#222222"
        assert ai.color in DEFAULT_PALETTE

    def test_override_with_invalid_color_falls_back(self):
        overrides = [{
            "model_id": "agent-1",
            "color": "not-a-color",
        }]
        ai = resolve_agent_identity("agent-1", "Qwen", overrides)
        assert ai.color in DEFAULT_PALETTE

    def test_override_for_different_agent_ignored(self):
        overrides = [{
            "model_id": "other-agent",
            "color": "#ef4444",
            "avatar": "🧪",
        }]
        ai = resolve_agent_identity("agent-1", "Qwen", overrides)
        # Ha de fer servir fallbacks perquè l'override és per un altre agent
        assert ai.color in DEFAULT_PALETTE
        assert ai.avatar == "Q"

    def test_override_partial_only_color(self):
        overrides = [{
            "model_id": "agent-1",
            "color": "#10b981",
        }]
        ai = resolve_agent_identity("agent-1", "Qwen", overrides)
        assert ai.color == "#10b981"
        # Avatar i rol fan fallback
        assert ai.avatar == "Q"
        assert ai.role is None

    def test_empty_name_uses_agent_id(self):
        ai = resolve_agent_identity("agent-1", "", [])
        assert ai.name == "agent-1"
        assert ai.avatar == "A"

    def test_to_dict(self):
        ai = resolve_agent_identity("agent-1", "Qwen", [])
        d = ai.to_dict()
        assert "agent_id" in d
        assert "name" in d
        assert "color" in d
        assert "avatar" in d
        assert "role" in d


class TestResolveChannelIdentities:
    def test_resolves_multiple_agents(self):
        agents = ["a1", "a2", "a3"]
        names = {"a1": "Qwen", "a2": "Claude", "a3": "Codex"}
        result = resolve_channel_identities(agents, names, [])
        assert len(result) == 3
        assert all(isinstance(ai, AgentIdentity) for ai in result)
        assert result[0].name == "Qwen"
        assert result[1].name == "Claude"
        assert result[2].name == "Codex"

    def test_missing_name_falls_back_to_agent_id(self):
        result = resolve_channel_identities(["a1"], {}, [])
        assert result[0].name == "a1"

    def test_overrides_applied_per_agent(self):
        agents = ["a1", "a2"]
        names = {"a1": "Qwen", "a2": "Claude"}
        overrides = [
            {"model_id": "a1", "color": "#10b981", "avatar": "🧪"},
            {"model_id": "a2", "color": "#ec4899", "avatar": "🎨"},
        ]
        result = resolve_channel_identities(agents, names, overrides)
        assert result[0].color == "#10b981"
        assert result[0].avatar == "🧪"
        assert result[1].color == "#ec4899"
        assert result[1].avatar == "🎨"

    def test_empty_agents_list(self):
        result = resolve_channel_identities([], {}, [])
        assert result == []

    def test_different_agents_get_different_colors(self):
        """Agents amb noms diferents haurien de tenir colors diferents (probabilístic)."""
        agents = ["a1", "a2", "a3", "a4"]
        names = {"a1": "Alpha", "a2": "Beta", "a3": "Gamma", "a4": "Delta"}
        result = resolve_channel_identities(agents, names, [])
        colors = {ai.color for ai in result}
        assert len(colors) >= 2
