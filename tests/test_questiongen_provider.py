# Loop closures are invoked and awaited within the current test iteration.
# ruff: noqa: B023
"""OpenRouter wire-boundary tests; every HTTP request uses MockTransport."""

import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from app.question_generation.provider import Completion, OpenRouterProvider, ProviderFailure
from app.question_generation.settings import GenerationSettings

KEY = "sk-test-offline-secret"
SETTINGS = GenerationSettings()
BODY = {
    "model": SETTINGS.writer.model,
    "messages": [{"role": "user", "content": "Вопрос"}],
    "max_tokens": 1000,
}


def envelope():
    return {
        "choices": [{"message": {"content": '{"text":"Вопрос"}'}, "finish_reason": "stop"}],
        "usage": {"cost": 0.012},
        "model": SETTINGS.fallback_writer.model,
        "id": "generation-test",
    }


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self):
        self.closed = True


class GenerationProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch(
            "socket.socket.connect", side_effect=AssertionError("Live sockets forbidden")
        )
        network.start()
        self.addCleanup(network.stop)

    async def call(self, response, *, timeout=1):
        calls = []

        async def handler(request):
            calls.append(request)
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                return await OpenRouterProvider(KEY, client=client).complete(BODY, timeout)
            finally:
                self.assertEqual(len(calls), 1, "Adapter must not retry implicitly")

    async def test_success_preserves_completion_and_sends_expected_request(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=envelope())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OpenRouterProvider(KEY, client=client).complete(BODY, 17)
            self.assertFalse(client.is_closed, "Injected client belongs to caller")
        self.assertEqual(
            result,
            Completion(
                '{"text":"Вопрос"}',
                0.012,
                SETTINGS.fallback_writer.model,
                "stop",
                "generation-test",
            ),
        )
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(json.loads(request.content), BODY)
        self.assertEqual(request.extensions["timeout"]["read"], 17)
        self.assertEqual(request.extensions["timeout"]["connect"], 10)

    async def test_null_or_malformed_choices_preserve_known_charge(self):
        for choices in (None, [], {}, "invalid", [None], [7], [{}]):
            with self.subTest(choices=choices):
                data = {**envelope(), "choices": choices}
                result = await self.call(httpx.Response(200, json=data))
                self.assertEqual(result.content, "")
                self.assertEqual(result.finish_reason, "unknown")
                self.assertEqual(result.cost_usd, 0.012)

    async def test_null_message_or_nontext_content_is_empty_not_stringified(self):
        for message in (
            None,
            [],
            "invalid",
            {},
            {"content": None},
            {"content": [{"type": "text", "text": "answer"}]},
            {"content": 42},
        ):
            with self.subTest(message=message):
                data = envelope()
                data["choices"][0]["message"] = message
                result = await self.call(httpx.Response(200, json=data))
                self.assertEqual(result.content, "")
                self.assertEqual(result.cost_usd, 0.012)
                self.assertEqual(result.finish_reason, "stop")

    async def test_null_or_malformed_usage_means_unknown_cost(self):
        for usage in (
            None,
            [],
            "invalid",
            {},
            {"cost": None},
            {"cost": True},
            {"cost": -1},
            {"cost": "0.012"},
            {"cost": []},
        ):
            with self.subTest(usage=usage):
                result = await self.call(httpx.Response(200, json={**envelope(), "usage": usage}))
                self.assertIsNone(result.cost_usd)
                self.assertEqual(result.content, envelope()["choices"][0]["message"]["content"])

    async def test_nonfinite_cost_is_unknown_and_zero_is_known(self):
        for cost in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(cost=cost):
                data = {**envelope(), "usage": {"cost": cost}}
                result = await self.call(httpx.Response(200, content=json.dumps(data).encode()))
                self.assertIsNone(result.cost_usd)
        result = await self.call(httpx.Response(200, json={**envelope(), "usage": {"cost": 0}}))
        self.assertEqual(result.cost_usd, 0)
        self.assertIsNotNone(result.cost_usd)

    async def test_null_metadata_uses_safe_defaults(self):
        data = envelope()
        data.update(model=None, id=None)
        data["choices"][0]["finish_reason"] = None
        result = await self.call(httpx.Response(200, json=data))
        self.assertEqual(result.model, BODY["model"])
        self.assertEqual(result.generation_id, "")
        self.assertEqual(result.finish_reason, "unknown")
        self.assertEqual(result.cost_usd, 0.012)

    async def test_invalid_model_metadata_falls_back_to_requested_model(self):
        for model in ("", "   ", "x" * 129, [], 3):
            with self.subTest(model=model):
                result = await self.call(httpx.Response(200, json={**envelope(), "model": model}))
                self.assertEqual(result.model, BODY["model"])

    async def test_missing_optional_metadata_and_usage_are_tolerated(self):
        data = {"choices": envelope()["choices"]}
        result = await self.call(httpx.Response(200, json=data))
        self.assertEqual(result.model, BODY["model"])
        self.assertEqual(result.generation_id, "")
        self.assertIsNone(result.cost_usd)

    async def test_invalid_json_and_utf8_are_uncertain_provider_failures(self):
        for body in (b"{broken", b"", b"\xff"):
            with self.subTest(body=body):
                with self.assertRaises(ProviderFailure) as caught:
                    await self.call(httpx.Response(200, content=body))
                self.assertEqual(caught.exception.code, "invalid_provider_response")
                self.assertTrue(caught.exception.retryable)
                self.assertFalse(caught.exception.no_charge)

    async def test_nonobject_envelopes_are_uncertain(self):
        for data in (None, [], "invalid"):
            with self.subTest(data=data):
                with self.assertRaises(ProviderFailure) as caught:
                    await self.call(
                        httpx.Response(200, json=data)
                        if data is not None
                        else httpx.Response(200, content=b"null")
                    )
                self.assertEqual(caught.exception.code, "provider_error")
                self.assertTrue(caught.exception.retryable)
                self.assertFalse(caught.exception.no_charge)
                self.assertNotIn(KEY, str(caught.exception))

    async def test_http_200_error_envelope_preserves_charge_and_generation_id(self):
        for cost in (0.012, 0, None):
            with self.subTest(cost=cost):
                data = {**envelope(), "error": {"message": "Echo " + KEY}, "usage": {"cost": cost}}
                result = await self.call(httpx.Response(200, json=data))
                self.assertEqual(
                    result, Completion("", cost, BODY["model"], "error", "generation-test")
                )
                self.assertNotIn(KEY, repr(result))
        result = await self.call(httpx.Response(200, json={"error": {"message": KEY}, "id": None}))
        self.assertEqual(result, Completion("", None, BODY["model"], "error", ""))

    async def test_http_status_retry_and_charge_classification(self):
        for status, retryable, no_charge in (
            (401, False, True),
            (429, True, True),
            (503, True, False),
        ):
            with self.subTest(status=status):
                with self.assertRaises(ProviderFailure) as caught:
                    await self.call(httpx.Response(status, text="sensitive echo " + KEY))
                self.assertEqual(caught.exception.code, f"http_{status}")
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertEqual(caught.exception.no_charge, no_charge)
                self.assertNotIn(KEY, str(caught.exception))

    async def test_stream_size_limit_stops_reading_and_closes_response(self):
        stream = Chunks([b" " * 1_000_000, b" " * 1_000_001, b"must not be read"])
        with self.assertRaises(ProviderFailure) as caught:
            await self.call(httpx.Response(200, stream=stream))
        self.assertEqual(caught.exception.code, "response_too_large")
        self.assertFalse(caught.exception.no_charge)
        self.assertEqual(stream.reads, 2)
        self.assertTrue(stream.closed)

    async def test_exact_body_size_limit_is_accepted(self):
        raw = json.dumps(envelope(), ensure_ascii=False).encode()
        stream = Chunks([raw, b" " * (2_000_000 - len(raw))])
        result = await self.call(httpx.Response(200, stream=stream))
        self.assertEqual(result.cost_usd, 0.012)
        self.assertTrue(stream.closed)

    async def test_secret_redaction_in_returned_content_and_metadata(self):
        data = copy.deepcopy(envelope())
        data["choices"][0]["message"]["content"] = "Эхо: " + KEY
        data["id"] = KEY
        data["model"] = "writer/" + KEY
        result = await self.call(httpx.Response(200, json=data))
        self.assertNotIn(KEY, repr(result))
        self.assertEqual(result.content, "Эхо: [REDACTED]")
        self.assertEqual(result.generation_id, "[REDACTED]")

    async def test_transport_and_timeout_failures_are_uncertain_without_secret_details(self):
        for error, code in (
            (httpx.ConnectError(KEY), "transport_uncertain"),
            (httpx.ReadTimeout(KEY), "timeout_uncertain"),
        ):
            with self.subTest(code=code):
                calls = []

                def handler(request):
                    calls.append(request)
                    raise error

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(ProviderFailure) as caught:
                        await OpenRouterProvider(KEY, client=client).complete(BODY, 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.retryable)
                self.assertFalse(caught.exception.no_charge)
                self.assertNotIn(KEY, str(caught.exception))

    async def test_total_timeout_interrupts_stream_and_closes_it(self):
        stream = WaitingStream()
        with self.assertRaises(ProviderFailure) as caught:
            await self.call(httpx.Response(200, stream=stream), timeout=0.02)
        self.assertEqual(caught.exception.code, "timeout_uncertain")
        self.assertFalse(caught.exception.no_charge)
        self.assertTrue(stream.started.is_set())
        self.assertTrue(stream.closed)

    async def test_task_cancellation_propagates_and_closes_stream(self):
        stream = WaitingStream()
        task = asyncio.create_task(self.call(httpx.Response(200, stream=stream), timeout=30))
        try:
            await asyncio.wait_for(stream.started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(stream.closed)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_invalid_credentials_rejected_before_http(self):
        for key in ("", " ", "key with spaces", "key\n"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                OpenRouterProvider(key)


if __name__ == "__main__":
    unittest.main()
