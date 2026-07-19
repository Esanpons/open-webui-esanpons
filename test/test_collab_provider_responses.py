import asyncio
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse

from open_webui.collab import orchestrator
from open_webui.collab.agents_status import extract_model_error


def test_successful_answer_can_discuss_quota_without_becoming_an_error():
    content = (
        "La resposta és correcta. Qwen està fallant perquè OpenRouter retorna "
        "quota/rate-limit, però Claude i Codex continuen operatius."
    )

    assert extract_model_error(content) is None


def test_provider_error_message_is_not_counted_as_a_successful_turn():
    detail = extract_model_error("Error: `tool calling` is not supported with this model")

    assert detail == "Error: `tool calling` is not supported with this model"


def test_retry_after_is_extracted_only_from_rate_limit_errors():
    assert orchestrator._retry_after_seconds(
        "Error: Rate limit reached. Please try again in 3.225s."
    ) == 3.225
    assert orchestrator._retry_after_seconds("Error: invalid model") is None


@pytest.mark.parametrize(
    "content, expected",
    [
        ("\n\n**Codex error:** usage limit reached", "**Codex error:**"),
        ("parcial\n\n**Claude error:** timed out", "**Claude error:**"),
        (
            "\n\n**Codex no ha retornat resposta.** Revisa el CLI.",
            "**Codex no ha retornat resposta.**",
        ),
    ],
)
def test_explicit_cli_error_blocks_are_detected(content, expected):
    detail = extract_model_error(content)

    assert detail is not None
    assert detail.startswith(expected)


def test_normalize_openai_dict():
    async def scenario():
        content, payload = await orchestrator._normalize_completion_response(
            {"choices": [{"message": {"content": "hola"}}], "usage": {"prompt_tokens": 2}}
        )
        assert content == "hola"
        assert payload["usage"]["prompt_tokens"] == 2

    asyncio.run(scenario())


def test_normalize_json_response_and_responses_api():
    async def scenario():
        response = JSONResponse(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "resposta local"}],
                    }
                ]
            }
        )
        content, _ = await orchestrator._normalize_completion_response(response)
        assert content == "resposta local"

    asyncio.run(scenario())


def test_normalize_plain_text_response():
    async def scenario():
        content, payload = await orchestrator._normalize_completion_response(
            PlainTextResponse("text del pipe")
        )
        assert content == "text del pipe"
        assert payload == {}

    asyncio.run(scenario())


def test_normalize_streaming_sse_accumulates_deltas():
    async def chunks():
        yield b'data: {"choices":[{"delta":{"content":"bon"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":" dia"}}]}\n\n'
        yield b'data: [DONE]\n\n'

    async def scenario():
        content, _ = await orchestrator._normalize_completion_response(
            StreamingResponse(chunks(), media_type="text/event-stream")
        )
        assert content == "bon dia"

    asyncio.run(scenario())


def test_normalize_provider_error_keeps_real_detail():
    async def scenario():
        with pytest.raises(RuntimeError, match="free-model rate limit reached"):
            await orchestrator._normalize_completion_response(
                JSONResponse(
                    {"error": {"message": "free-model rate limit reached"}},
                    status_code=429,
                )
            )

    asyncio.run(scenario())


def test_normalize_openrouter_error_surfaces_code_provider_and_raw():
    async def scenario():
        with pytest.raises(RuntimeError) as excinfo:
            await orchestrator._normalize_completion_response(
                JSONResponse(
                    {
                        "error": {
                            "message": "Provider returned error",
                            "code": 429,
                            "metadata": {
                                "provider_name": "Chutes",
                                "raw": "qwen3-coder:free rate-limited, retry later",
                            },
                        }
                    },
                    status_code=429,
                )
            )
        detail = str(excinfo.value)
        assert "Provider returned error" in detail
        assert "[codi 429]" in detail
        assert "Chutes" in detail
        assert "rate-limited" in detail

    asyncio.run(scenario())


def test_run_generation_consumes_stream_and_finalizes_plain_fallback(monkeypatch):
    async def scenario():
        consumed = []

        async def chunks():
            consumed.append(True)
            yield b"resposta sense events"

        async def handler(_request, _form_data, user=None):
            return StreamingResponse(chunks(), media_type="text/plain")

        stored = SimpleNamespace(content="", meta={"model_id": "local"}, data={})

        async def get_message(_message_id):
            return stored

        async def update_message(_message_id, form):
            stored.content = form.content
            stored.meta = form.meta
            stored.data = form.data
            return stored

        monkeypatch.setattr(orchestrator.Messages, "get_message_by_id", get_message)
        monkeypatch.setattr(orchestrator.Messages, "update_message_by_id", update_message)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(CHAT_COMPLETION_HANDLER=handler))
        )

        content = await orchestrator._run_generation_until_done(request, {}, None, "m1")

        assert consumed == [True]
        assert content == "resposta sense events"
        assert stored.content == content
        assert stored.meta["done"] is True

    asyncio.run(scenario())
