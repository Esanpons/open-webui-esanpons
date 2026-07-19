"""El guardrail de canal és la font única del timeout dels pipes CLI."""

from types import SimpleNamespace

from integrations.claude_cli_pipe import _effective_timeout as claude_timeout
from integrations.codex_pipe import _effective_timeout as codex_timeout
from open_webui.collab.config import CollabConfig
from open_webui.collab.context import collab_generation_context


def test_normal_chats_keep_pipe_timeout():
    assert codex_timeout({}, 300, 570) == 300
    assert claude_timeout({}, 300, 570) == 300


def test_legacy_collab_context_uses_fallback_valve():
    assert codex_timeout({"channel_id": "c1"}, 300, 570) == 570
    assert claude_timeout({"channel_id": "c1"}, 300, 570) == 570


def test_collab_guardrail_accepts_any_positive_seconds():
    assert codex_timeout({"turn_timeout": 75}, 300, 570) == 75
    assert claude_timeout({"turn_timeout": 1200}, 300, 570) == 1200


def test_collab_guardrail_zero_means_unlimited():
    assert codex_timeout({"turn_timeout": 0}, 300, 570) is None
    assert claude_timeout({"turn_timeout": "0"}, 300, 570) is None


def test_generation_context_reaches_tools_and_pipes():
    payload = collab_generation_context(
        SimpleNamespace(id="c1"),
        CollabConfig(guardrails={"turn_timeout": 900}),
        "turn-1",
    )
    variables = payload["variables"]["collab"]
    metadata = payload["metadata"]["collab"]
    assert variables == metadata
    assert metadata["channel_id"] == "c1"
    assert metadata["turn_id"] == "turn-1"
    assert metadata["turn_timeout"] == 900
