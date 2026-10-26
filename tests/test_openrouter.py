import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_openrouter_client
from app.clients.openrouter import OpenRouterClient
from app.main import app
from app.prompts.evaluator import EVALUATOR_PROMPT_VERSION, EVALUATOR_SYSTEM_PROMPT
from app.prompts.interviewer import INTERVIEWER_PROMPT_VERSION, INTERVIEWER_SYSTEM_PROMPT
from app.schemas.evaluator import EvaluatorResponse
from app.schemas.interviewer import InterviewerResponse
from app.settings import Settings


@pytest.mark.parametrize(
    ("service", "schema", "temperature", "prompt", "version"),
    [
        (
            "interviewer",
            InterviewerResponse,
            0.2,
            INTERVIEWER_SYSTEM_PROMPT,
            INTERVIEWER_PROMPT_VERSION,
        ),
        ("evaluator", EvaluatorResponse, 0.0, EVALUATOR_SYSTEM_PROMPT, EVALUATOR_PROMPT_VERSION),
    ],
)
def test_request_construction(
    client,
    mock_router,
    interviewer_payload,
    evaluator_payload,
    interviewer_scenarios,
    service,
    schema,
    temperature,
    prompt,
    version,
):
    payload = interviewer_payload if service == "interviewer" else evaluator_payload
    injection = interviewer_scenarios["injection"]
    payload["dialogue"] = [
        {"role": "candidate", "content": injection},
        {"role": "interviewer", "content": "Обоснуйте своё утверждение."},
        {"role": "candidate", "content": "Группирует строки."},
    ]
    assert client.post(f"/api/v1/{service}", json=payload).status_code == 200
    call = mock_router.calls[0]
    assert call["model"] == "qwen/qwen3.5-27b"
    assert call["temperature"] == temperature
    assert call["provider"] == {
        "order": ["alibaba"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert call["reasoning"] == {"enabled": False}
    assert call["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "strict": True,
            "schema": schema.model_json_schema(),
        },
    }
    assert set(call) == {
        "model",
        "messages",
        "temperature",
        "provider",
        "reasoning",
        "response_format",
    }
    messages = call["messages"]
    assert prompt and version
    assert messages[0] == {"role": "system", "content": prompt}
    trusted = json.loads(messages[1]["content"].split("\n", 1)[1])
    assert trusted == {k: v for k, v in payload.items() if k != "dialogue"}
    assert all(injection not in message["content"] for message in messages[:2])
    assert messages[2:] == [
        {"role": "user", "content": injection},
        {"role": "assistant", "content": "Обоснуйте своё утверждение."},
        {"role": "user", "content": "Группирует строки."},
    ]
    if service == "interviewer":
        assert "reference_answer" not in trusted
    else:
        assert trusted["reference_answer"] == payload["reference_answer"]
        assert trusted["criteria"][1]["anchors"] == payload["criteria"][1]["anchors"]
    request = mock_router.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer unit-test-placeholder"
    assert all(value == 55 for value in request.extensions["timeout"].values())


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, 502),
        (401, 503),
        (402, 503),
        (403, 503),
        (404, 503),
        (408, 504),
        (429, 503),
        (500, 503),
        (502, 503),
        (503, 503),
        (504, 504),
    ],
)
@pytest.mark.parametrize("service", ["interviewer", "evaluator"])
def test_upstream_http_errors_are_safe_and_not_retried(
    client,
    mock_router,
    interviewer_payload,
    evaluator_payload,
    status,
    expected,
    service,
):
    marker = "upstream-private-error-content"
    mock_router.responses.append(httpx.Response(status, json={"error": marker}))
    payload = interviewer_payload if service == "interviewer" else evaluator_payload
    response = client.post(f"/api/v1/{service}", json=payload)
    assert response.status_code == expected
    assert marker not in response.text
    assert "unit-test-placeholder" not in response.text
    assert len(mock_router.calls) == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("private-timeout"), 504),
        (httpx.ConnectTimeout("private-timeout"), 504),
        (httpx.ConnectError("private-network-error"), 503),
        (httpx.ReadError("private-network-error"), 503),
    ],
)
def test_transport_errors(client, mock_router, interviewer_payload, error, expected):
    mock_router.responses.append(error)
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == expected
    assert "private-" not in response.text
    assert len(mock_router.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": []}, "finish_reason": "stop"}]},
        {"choices": [{"message": {}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]},
        {"choices": [{"message": {"content": "{}"}, "finish_reason": []}]},
        {"error": {"code": "bad", "message": "private-raw-body"}},
    ],
)
def test_invalid_envelopes(client, mock_router, interviewer_payload, body):
    mock_router.responses.append(httpx.Response(200, json=body))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 502
    assert "private-raw-body" not in response.text
    assert len(mock_router.calls) == 1


def test_non_json_upstream(client, mock_router, interviewer_payload):
    mock_router.responses.append(httpx.Response(200, text="private-non-json-upstream"))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 502
    assert "private-non-json" not in response.text
    assert len(mock_router.calls) == 1


def test_error_in_success_envelope(client, mock_router, interviewer_payload):
    mock_router.responses.append(httpx.Response(200, json={"error": {"code": 429}}))
    assert client.post("/api/v1/interviewer", json=interviewer_payload).status_code == 503
    assert len(mock_router.calls) == 1


def test_observability_has_usage_and_no_content(client, mock_router, evaluator_payload, caplog):
    evaluator_payload["reference_answer"] = "private-reference-marker"
    with caplog.at_level(logging.INFO):
        response = client.post("/api/v1/evaluator", json=evaluator_payload)
    assert response.status_code == 200
    logs = "\n".join(r.getMessage() for r in caplog.records)
    for expected in (
        "service=evaluator",
        "model=qwen/qwen3.5-27b",
        "provider=alibaba",
        f"prompt_version={EVALUATOR_PROMPT_VERSION}",
        "attempt=1",
        "latency_ms=",
        "http_status=200",
        "outcome=success",
        "finish_reason=stop",
        "input_tokens=120",
        "output_tokens=30",
        "reasoning_tokens=0",
    ):
        assert expected in logs
    for private in (
        "private-reference-marker",
        evaluator_payload["dialogue"][0]["content"],
        EVALUATOR_SYSTEM_PROMPT,
        "unit-test-placeholder",
        "Authorization",
    ):
        assert private not in logs


def test_settings_load_dotenv_and_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENROUTER_API_KEY=dotenv-placeholder\n", encoding="utf-8")
    settings = Settings(_env_file=dotenv)
    assert settings.openrouter_api_key.get_secret_value() == "dotenv-placeholder"
    assert "dotenv-placeholder" not in repr(settings)
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-placeholder")
    assert (
        Settings(_env_file=dotenv).openrouter_api_key.get_secret_value()
        == "environment-placeholder"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"openrouter_timeout_seconds": 0},
        {"openrouter_timeout_seconds": float("inf")},
        {"openrouter_provider": "another-provider"},
        {"openrouter_model": "another-model"},
    ],
)
def test_invalid_settings(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_missing_key_keeps_app_healthy_and_returns_503(
    mock_router,
    interviewer_payload,
    evaluator_payload,
):
    settings = Settings(_env_file=None, openrouter_api_key="")
    unconfigured = OpenRouterClient(mock_router.http, settings)
    app.dependency_overrides[get_openrouter_client] = lambda: unconfigured
    try:
        with TestClient(app) as test_client:
            assert test_client.get("/health").status_code == 200
            assert test_client.get("/docs").status_code == 200
            assert test_client.get("/openapi.json").status_code == 200
            for service, payload in (
                ("interviewer", interviewer_payload),
                ("evaluator", evaluator_payload),
            ):
                response = test_client.post(f"/api/v1/{service}", json=payload)
                assert response.status_code == 503
                assert response.json() == {"detail": "OPENROUTER_API_KEY is not configured"}
            interviewer_payload["follow_up_count"] = interviewer_payload["max_follow_ups"]
            assert test_client.post("/api/v1/interviewer", json=interviewer_payload).json() == {
                "action": "complete",
                "message": None,
            }
        assert mock_router.calls == []
    finally:
        app.dependency_overrides.pop(get_openrouter_client, None)
