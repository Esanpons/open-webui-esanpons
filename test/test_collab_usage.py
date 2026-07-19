import asyncio
import importlib.metadata

import pytest

# El checkout no està instal·lat com a paquet dins l'entorn de prova. El codi
# només necessita una versió informativa durant la importació d'open_webui.env.
_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

from open_webui.collab.usage import (
    MAX_ERROR_DETAIL,
    STATUS_CLI,
    STATUS_CONTEXT,
    STATUS_EMPTY,
    STATUS_PROVIDER,
    STATUS_QUOTA,
    STATUS_TIMEOUT,
    classify_error,
    estimate_tokens,
    sanitize_error_detail,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTP 429 insufficient_quota", STATUS_QUOTA),
        ("context_length_exceeded: maximum context window", STATUS_CONTEXT),
        (asyncio.TimeoutError("slow"), STATUS_TIMEOUT),
        ("**Codex error:** no ha retornat resposta", STATUS_CLI),
        ("503 service unavailable", STATUS_PROVIDER),
        ("", STATUS_EMPTY),
    ],
)
def test_classify_error_categories(raw, expected):
    status, _detail = classify_error(raw)
    assert status == expected


def test_sanitize_error_detail_redacts_and_limits_secrets():
    raw = "authorization=Bearer-secret token=super-secret " + ("x" * 500)
    detail = sanitize_error_detail(raw)
    assert "super-secret" not in detail
    assert len(detail) <= MAX_ERROR_DETAIL


def test_estimate_tokens_handles_empty_and_nonempty_text():
    assert estimate_tokens(None) == 0
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
