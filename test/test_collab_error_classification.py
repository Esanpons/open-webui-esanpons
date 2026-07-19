"""Test de contracte de la classificació d'errors (MR-25).

`usage.classify_error` és el classificador central de tots els errors de model
(6 categories + èxit). Aquest test el fixa contra cadenes REALS que emeten els
proveïdors i els pipes CLI: si un canvi de redacció d'un proveïdor o de les
regex trenca una categoria, aquí es veu de seguida, en comptes de degradar-se
silenciosament a `provider_error` amb un retry incorrecte.

També cobreix `agents_status.extract_model_error` (detecció del bloc d'error
que emeten els pipes Claude/Codex) i la redacció de credencials.
"""

import importlib.metadata

_real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.0.0" if name == "open-webui" else _real_version(name)

import asyncio

from open_webui.collab.usage import (
    STATUS_CLI,
    STATUS_CONTEXT,
    STATUS_EMPTY,
    STATUS_PROVIDER,
    STATUS_QUOTA,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    classify_error,
    sanitize_error_detail,
)
from open_webui.collab.agents_status import extract_model_error


# (cadena real del proveïdor/pipe, status esperat)
QUOTA_CASES = [
    "Error: 429 Too Many Requests",
    "You exceeded your current quota, please check your plan and billing details.",
    "Rate limit reached for gpt-4o. Please try again in 20s.",
    "insufficient_quota: your credit balance is too low",
    "Groq: rate_limit_exceeded — tokens per minute (TPM) reached",
]

CONTEXT_CASES = [
    "This model's maximum context length is 128000 tokens.",
    "Error 413: Request Entity Too Large",
    "prompt is too long: 210000 tokens > 200000 maximum",
    "input is too long for the requested model",
]

PROVIDER_CASES = [
    "Provider returned HTTP 500",
    "502 Bad Gateway",
    "Service Unavailable",
    "connection reset by peer",
    "Internal Server Error",
]


def test_quota_strings_classify_as_quota():
    for text in QUOTA_CASES:
        status, detail = classify_error(text)
        assert status == STATUS_QUOTA, f"{text!r} → {status}"
        assert detail  # es conserva un detall sanejat


def test_context_strings_classify_as_context():
    for text in CONTEXT_CASES:
        status, _ = classify_error(text)
        assert status == STATUS_CONTEXT, f"{text!r} → {status}"


def test_provider_strings_classify_as_provider():
    for text in PROVIDER_CASES:
        status, _ = classify_error(text)
        assert status == STATUS_PROVIDER, f"{text!r} → {status}"


def test_timeout_exception_and_text():
    assert classify_error(asyncio.TimeoutError("turn_timeout"))[0] == STATUS_TIMEOUT
    assert classify_error("The request timed out")[0] == STATUS_TIMEOUT


def test_cli_pipe_error_block_classifies_as_cli():
    # Bloc real que emet el pipe de Claude Code / Codex. Nota: la quota guanya
    # sobre CLI per ordre (un "**Claude error:** quota..." es classifica com a
    # quota, cosa desitjada); aquí provem un error de CLI sense quota.
    text = "Resposta parcial…\n**Codex error:** el procés ha finalitzat amb codi 1"
    assert classify_error(text)[0] == STATUS_CLI


def test_empty_is_empty_response():
    assert classify_error("")[0] == STATUS_EMPTY
    assert classify_error(None)[0] == STATUS_EMPTY
    assert classify_error("   \n  ")[0] == STATUS_EMPTY


def test_unknown_falls_back_to_provider():
    status, _ = classify_error("alguna cosa rara que no encaixa enlloc")
    assert status == STATUS_PROVIDER


def test_success_marker():
    # classify_error mai retorna success (és per errors); la constant existeix.
    assert STATUS_SUCCESS == "success"


def test_quota_wins_over_timeout():
    # Molts errors de quota mencionen "retry"/"try again"; han de ser quota.
    status, _ = classify_error("Rate limit reached. Please try again in 12s")
    assert status == STATUS_QUOTA


# ---------------------------------------------------------------------------
# extract_model_error: només detecta el bloc d'error explícit del pipe, no una
# menció casual dins d'una resposta normal.
# ---------------------------------------------------------------------------


def test_extract_model_error_detects_pipe_block():
    content = "He fet la meva part.\n**Codex error:** timeout after 900s"
    extracted = extract_model_error(content)
    assert extracted is not None
    assert "Codex error" in extracted


def test_extract_model_error_ignores_casual_mention():
    # Un agent que MENCIONA un error d'un altre no ha de comptar com a fallada.
    content = "L'A2 ha tingut un **problema** però jo he acabat la feina bé."
    assert extract_model_error(content) is None


def test_extract_generic_error_prefix():
    assert extract_model_error("Error: boom") is not None
    assert extract_model_error("Exception: kaboom") is not None


# ---------------------------------------------------------------------------
# Sanejament: mai credencials al detall d'error.
# ---------------------------------------------------------------------------


def test_sanitize_redacts_credentials():
    out = sanitize_error_detail("auth failed with key sk-abcdef1234567890 and bearer XYZ123abc456")
    assert "sk-abcdef" not in out
    assert "[redacted]" in out


def test_sanitize_limits_length():
    out = sanitize_error_detail("x" * 5000)
    assert out is not None and len(out) <= 300
