import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import completion_response, contract_validator

from app.clients.openrouter import OpenRouterClient
from app.generation import GenerationContext
from app.main import app
from app.schemas.interviewer import InterviewerRequest
from app.services.interviewer import generate_interviewer_turn
from app.settings import Settings


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 1000.0
        utc = datetime(2030, 1, 1, tzinfo=UTC)

        def advance(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr("app.generation.perf_counter", lambda: clock.now)
    monkeypatch.setattr("app.generation.utc_now", lambda: clock.utc)
    return clock


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
@pytest.mark.parametrize("seconds,expected", [(None, 55), (60, 55), (30, 25), (120, 55)])
def test_request_budget_and_http_timeout(
    client, mock_router, interviewer_payload, evaluator_payload, clock, operation, seconds, expected
):
    payload = interviewer_payload if operation == "interviewer" else evaluator_payload
    if seconds is not None:
        payload["deadline_at"] = (clock.utc + timedelta(seconds=seconds)).isoformat()
    contract_validator(operation, "input").validate(payload)
    response = client.post(f"/api/v1/{operation}", json=payload)
    assert response.status_code == 200
    timeouts = mock_router.requests[0].extensions["timeout"]
    assert all(value == expected for value in timeouts.values())
    trusted = mock_router.calls[0]["messages"][1]["content"]
    assert "deadline_at" not in trusted
    assert mock_router.calls[0]["response_format"]["type"] == "json_schema"
    assert "meta" not in json.loads(trusted.split("\n", 1)[1])


@pytest.mark.parametrize("seconds", [-1, 0, 4.9, 5])
def test_deadline_reserve_prevents_any_provider_call(
    client, mock_router, interviewer_payload, clock, seconds
):
    interviewer_payload["deadline_at"] = (clock.utc + timedelta(seconds=seconds)).isoformat()
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "ML_TIMEOUT"
    assert response.json()["meta"]["provider_attempts"] == 0
    assert response.json()["meta"]["prompt_tokens"] == 0
    assert mock_router.calls == []


@pytest.mark.parametrize(
    "date",
    [
        None,
        123,
        "2030-01-01",
        "2030-01-01T10:00:00",
        "2030-01-01T10:00:00+03:00",
        "2030-01-01T10:00:00-00:00",
        "2030-02-31T10:00:00Z",
        "invalid",
    ],
)
@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
def test_invalid_deadline_is_input_error(
    client, mock_router, interviewer_payload, evaluator_payload, operation, date
):
    payload = interviewer_payload if operation == "interviewer" else evaluator_payload
    payload["deadline_at"] = date
    response = client.post(f"/api/v1/{operation}", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ML_INVALID_INPUT"
    assert mock_router.calls == []


@pytest.mark.parametrize(
    "date", ["2030-01-01T00:01:00Z", "2030-01-01T00:01:00+00:00", "2030-01-01t00:01:00z"]
)
def test_rfc3339_utc_forms_match_input_schema(client, interviewer_payload, clock, date):
    interviewer_payload["deadline_at"] = date
    contract_validator("interviewer", "input").validate(interviewer_payload)
    assert client.post("/api/v1/interviewer", json=interviewer_payload).status_code == 200


@pytest.mark.parametrize("spent,expected_calls,status", [(54, 2, 200), (55, 1, 504), (56, 1, 504)])
def test_repair_uses_original_budget_and_never_starts_after_deadline(
    client, mock_router, interviewer_payload, monkeypatch, clock, spent, expected_calls, status
):
    original = mock_router.client.complete
    calls = []

    async def complete(**kwargs):
        calls.append(True)
        value = await original(**kwargs)
        if len(calls) == 1:
            clock.advance(spent)
        return value

    monkeypatch.setattr(mock_router.client, "complete", complete)
    mock_router.queue("invalid JSON", {"action": "complete", "message": None})
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == status
    assert len(mock_router.calls) == expected_calls
    assert response.json()["meta"]["provider_attempts"] == expected_calls
    if expected_calls == 2:
        assert set(mock_router.requests[1].extensions["timeout"].values()) == {1.0}
    else:
        assert response.json()["error"]["code"] == "ML_TIMEOUT"


def test_duration_is_wall_clock_not_a_constant_or_token_sum(
    client, mock_router, interviewer_payload, clock, monkeypatch
):
    original = mock_router.client.complete

    async def complete(**kwargs):
        result = await original(**kwargs)
        clock.advance(12.7)
        return result

    monkeypatch.setattr(mock_router.client, "complete", complete)
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert abs(response.json()["meta"]["duration_ms"] - 12700) <= 1


def test_wall_clock_jump_does_not_change_monotonic_deadline(clock):
    context = GenerationContext("interviewer")
    context.configure_deadline(55, clock.utc + timedelta(seconds=30))
    clock.utc += timedelta(days=100)
    clock.advance(10)
    assert context.remaining() == 15


def test_unrelated_timeout_exception_is_internal(client, mock_router, interviewer_payload):
    mock_router.responses.append(TimeoutError("PRIVATE-APPLICATION-ERROR"))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "ML_INTERNAL_ERROR"
    assert "PRIVATE" not in response.text


def test_external_cancellation_is_not_converted_to_timeout(interviewer_payload):
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def respond(request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        context = GenerationContext("interviewer")
        settings = Settings(_env_file=None, openrouter_api_key="unit-test-placeholder")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            task = asyncio.create_task(
                generate_interviewer_turn(
                    InterviewerRequest.model_validate(interviewer_payload),
                    OpenRouterClient(http, settings),
                    context,
                )
            )
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert cancelled.is_set()
        assert len(context.usage.attempts) == 1

    asyncio.run(scenario())


def test_http_disconnect_cancels_provider_and_does_not_send_a_response(
    client, mock_router, interviewer_payload
):
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        sent = []

        async def respond(request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": json.dumps(interviewer_payload).encode()}
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/interviewer",
            "raw_path": b"/api/v1/interviewer",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 1),
            "server": ("test", 80),
        }
        original = mock_router.client.http
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            mock_router.client.http = http
            try:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(app(scope, receive, send), 2)
            finally:
                mock_router.client.http = original
        assert cancelled.is_set()
        assert sent == []

    asyncio.run(scenario())


def test_deadline_also_covers_final_http_result_formation(
    client, interviewer_payload, monkeypatch, clock
):
    original = json.loads

    def decode(body, *args, **kwargs):
        data = original(body, *args, **kwargs)
        if isinstance(data, dict) and "meta" in data and "action" in data:
            clock.advance(56)
        return data

    monkeypatch.setattr("app.http_contract.json.loads", decode)
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "ML_TIMEOUT"
    assert response.json()["meta"]["provider_attempts"] == 1


def test_concurrent_http_requests_have_independent_usage_and_repair_budgets(
    client, mock_router, interviewer_payload
):
    async def scenario():
        counts = {}

        async def respond(request):
            body = json.loads(request.content)
            question = json.loads(body["messages"][1]["content"].split("\n", 1)[1])["question"]
            counts[question] = counts.get(question, 0) + 1
            await asyncio.sleep(0)
            if question == "repair" and counts[question] == 1:
                return completion_response("invalid JSON")
            return completion_response({"action": "complete", "message": None})

        original = mock_router.client.http
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as provider:
            mock_router.client.http = provider
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://test"
                ) as api:
                    a, b = await asyncio.gather(
                        *[
                            api.post(
                                "/api/v1/interviewer", json={**interviewer_payload, "question": q}
                            )
                            for q in ["repair", "once"]
                        ]
                    )
            finally:
                mock_router.client.http = original
        assert a.status_code == b.status_code == 200
        assert a.json()["meta"]["provider_attempts"] == 2
        assert b.json()["meta"]["provider_attempts"] == 1
        assert a.json()["meta"]["prompt_tokens"] == 240
        assert b.json()["meta"]["prompt_tokens"] == 120
        assert a.headers["X-Correlation-ID"] != b.headers["X-Correlation-ID"]

    asyncio.run(scenario())
