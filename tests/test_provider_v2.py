import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from conftest import completion_response
from pydantic import SecretStr


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, None, "ML_PROVIDER_AUTH_ERROR"),
        (403, None, "ML_PROVIDER_AUTH_ERROR"),
        (402, None, "ML_PROVIDER_QUOTA_EXCEEDED"),
        (429, None, "ML_RATE_LIMITED"),
        (408, None, "ML_TIMEOUT"),
        (504, None, "ML_TIMEOUT"),
        (500, None, "ML_UNAVAILABLE"),
        (502, None, "ML_UNAVAILABLE"),
        (503, None, "ML_UNAVAILABLE"),
        (400, {"error": {"code": 400, "param": "model"}}, "ML_PROVIDER_CONFIG_ERROR"),
        (404, {"error": {"type": "model_not_found"}}, "ML_PROVIDER_CONFIG_ERROR"),
        (422, {"error": {"code": "unsupported_parameter"}}, "ML_PROVIDER_CONFIG_ERROR"),
        (400, {"error": {"message": "unknown"}}, "ML_INVALID_RESPONSE"),
        (404, None, "ML_INVALID_RESPONSE"),
        (418, None, "ML_INVALID_RESPONSE"),
        (200, {"error": {"code": 401}}, "ML_PROVIDER_AUTH_ERROR"),
        (200, {"error": {"code": "402"}}, "ML_PROVIDER_QUOTA_EXCEEDED"),
        (200, {"error": {"type": "insufficient_quota"}}, "ML_PROVIDER_QUOTA_EXCEEDED"),
        (200, {"error": {"code": "region_not_supported"}}, "ML_PROVIDER_AUTH_ERROR"),
        (200, {"error": {"code": 429}}, "ML_RATE_LIMITED"),
        (200, {"error": {"code": 504}}, "ML_TIMEOUT"),
        (200, {"error": {"code": 502}}, "ML_UNAVAILABLE"),
        (200, {"error": {"code": "unknown", "message": "quota exceeded"}}, "ML_INVALID_RESPONSE"),
        (200, {"choices": [{"error": {"code": 429}}]}, "ML_RATE_LIMITED"),
        (200, {"choices": [{"finish_reason": "unknown"}]}, "ML_INVALID_RESPONSE"),
        (200, {"choices": [{"finish_reason": "content_filter"}]}, "ML_POLICY_REFUSAL"),
        (
            200,
            {"choices": [{"finish_reason": "stop", "message": {"refusal": "refused"}}]},
            "ML_POLICY_REFUSAL",
        ),
    ],
)
def test_provider_classification_is_precise_and_has_no_network_retries(
    client, mock_router, interviewer_payload, evaluator_payload, operation, status, body, expected
):
    mock_router.responses.append(httpx.Response(status, json=body))
    payload = interviewer_payload if operation == "interviewer" else evaluator_payload
    response = client.post(f"/api/v1/{operation}", json=payload)
    assert response.json()["error"]["code"] == expected
    assert response.json()["meta"]["provider_attempts"] == 1
    assert len(mock_router.calls) == 1
    assert "Authorization" not in response.text
    assert "unit-test-placeholder" not in response.text


@pytest.mark.parametrize(
    "header,expected",
    [("10", 10), ("0", 0), ("-1", None), ("1.5", None), ("garbage", None), (None, None)],
)
def test_retry_after_header_and_details(client, mock_router, interviewer_payload, header, expected):
    headers = {} if header is None else {"Retry-After": header}
    mock_router.responses.append(httpx.Response(429, headers=headers))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    details = response.json()["error"]["details"]
    assert details["retryable"] is True
    assert details.get("retry_after_seconds") == expected
    assert response.headers.get("Retry-After") == (None if expected is None else str(expected))
    assert response.headers["X-Correlation-ID"] == details["correlation_id"]


def test_retry_after_http_date(client, mock_router, interviewer_payload):
    date = datetime.now(UTC) + timedelta(seconds=60)
    mock_router.responses.append(
        httpx.Response(429, headers={"Retry-After": format_datetime(date, usegmt=True)})
    )
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    value = response.json()["error"]["details"]["retry_after_seconds"]
    assert 58 <= value <= 60
    assert response.headers["Retry-After"] == str(value)


def invalid_generation(kind):
    reply = completion_response("not JSON")
    data = json.loads(reply.content)
    if kind == "empty":
        data["choices"][0]["message"]["content"] = None
    elif kind == "truncated":
        data["choices"][0].update(finish_reason="length", message={"content": "{}"})
    return httpx.Response(200, json=data)


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
@pytest.mark.parametrize("kind", ["json", "empty", "truncated"])
def test_only_repairable_generation_errors_get_one_repair(
    client, mock_router, interviewer_payload, evaluator_payload, operation, kind
):
    mock_router.responses.append(invalid_generation(kind))
    payload = interviewer_payload if operation == "interviewer" else evaluator_payload
    response = client.post(f"/api/v1/{operation}", json=payload)
    assert response.status_code == 200
    assert len(mock_router.calls) == 2
    assert response.json()["meta"]["provider_attempts"] == 2
    assert response.json()["meta"]["prompt_tokens"] == 240
    assert response.json()["meta"]["completion_tokens"] == 60
    first, second = mock_router.calls
    for field in ["model", "provider", "temperature", "response_format", "reasoning"]:
        assert first[field] == second[field]
    assert first["messages"][:3] == second["messages"][:3]
    assert first["messages"][3:] == second["messages"][4:]
    assert second["messages"][3]["role"] == "system"
    assert "not JSON" not in second["messages"][3]["content"]


@pytest.mark.parametrize(
    "kind,code",
    [
        ("json", "ML_SCHEMA_ERROR"),
        ("empty", "ML_EMPTY_RESPONSE"),
        ("truncated", "ML_OUTPUT_TRUNCATED"),
    ],
)
def test_repair_exhaustion_reports_final_code_and_both_calls(
    client, mock_router, interviewer_payload, kind, code
):
    mock_router.responses.extend([invalid_generation(kind), invalid_generation(kind)])
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"]["retryable"] is False
    assert response.json()["meta"]["provider_attempts"] == 2
    assert len(mock_router.calls) == 2


@pytest.mark.parametrize(
    "second,code",
    [
        (httpx.ReadTimeout("private"), "ML_TIMEOUT"),
        (httpx.Response(429), "ML_RATE_LIMITED"),
        (httpx.Response(401), "ML_PROVIDER_AUTH_ERROR"),
        (httpx.Response(500), "ML_UNAVAILABLE"),
    ],
)
def test_repair_reports_actual_second_error_and_keeps_unknown_usage(
    client, mock_router, interviewer_payload, second, code
):
    mock_router.responses.extend([invalid_generation("json"), second])
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.json()["error"]["code"] == code
    assert response.json()["meta"]["provider_attempts"] == 2
    assert response.json()["meta"]["prompt_tokens"] is None
    assert response.json()["meta"]["completion_tokens"] is None
    assert len(mock_router.calls) == 2


@pytest.mark.parametrize(
    "usage,expected",
    [
        (
            {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
            (7, 3, 1),
        ),
        (None, (None, None, None)),
        ([], (None, None, None)),
        ({"prompt_tokens": 0, "completion_tokens": 0}, (0, 0, None)),
        ({"prompt_tokens": 8}, (8, None, None)),
        ({"prompt_tokens": True, "completion_tokens": -1}, (None, None, None)),
        ({"prompt_tokens": "1", "completion_tokens": 1.5}, (None, None, None)),
    ],
)
def test_usage_is_factual_and_missing_values_are_unknown(
    client, mock_router, interviewer_payload, interviewer_result, usage, expected
):
    body = json.loads(completion_response(interviewer_result).content)
    body["usage"] = usage
    mock_router.responses.append(httpx.Response(200, json=body))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert (
        tuple(meta[k] for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"))
        == expected
    )
    assert "total_tokens" not in meta
    assert meta["provider_attempts"] == 1


@pytest.mark.parametrize(
    "first_usage,second_usage,expected",
    [
        (
            {"prompt_tokens": 10, "completion_tokens": 3},
            {"prompt_tokens": 20, "completion_tokens": 5},
            (30, 8, None),
        ),
        (None, {"prompt_tokens": 20, "completion_tokens": 5}, (None, None, None)),
        ({"prompt_tokens": 10}, {"prompt_tokens": 20, "completion_tokens": 5}, (30, None, None)),
        (
            {
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
            {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
            (30, 8, 3),
        ),
    ],
)
def test_aggregate_requires_every_started_attempt_to_report_each_metric(
    client,
    mock_router,
    interviewer_payload,
    interviewer_result,
    first_usage,
    second_usage,
    expected,
):
    bodies = [
        json.loads(invalid_generation("json").content),
        json.loads(completion_response(interviewer_result).content),
    ]
    for body, usage in zip(bodies, [first_usage, second_usage], strict=True):
        body["usage"] = usage
        mock_router.responses.append(httpx.Response(200, json=body))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert (
        tuple(meta[k] for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"))
        == expected
    )
    assert meta["provider_attempts"] == 2


def test_usage_survives_error_in_http_200_envelope(client, mock_router, interviewer_payload):
    mock_router.responses.append(
        httpx.Response(
            200,
            json={
                "error": {"code": 429},
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )
    )
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.json()["meta"]["prompt_tokens"] == 10
    assert response.json()["meta"]["completion_tokens"] == 2


def test_unconfigured_provider_has_zero_started_attempts(client, mock_router, interviewer_payload):
    mock_router.client.settings.openrouter_api_key = SecretStr("")
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.json()["error"]["code"] == "ML_PROVIDER_NOT_CONFIGURED"
    assert response.json()["meta"]["provider_attempts"] == 0
    assert response.json()["meta"]["prompt_tokens"] == 0
    assert mock_router.calls == []


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, {"error": {"code": 429}}, "ML_PROVIDER_AUTH_ERROR"),
        (402, {"error": {"code": 503}}, "ML_PROVIDER_QUOTA_EXCEEDED"),
        (429, {"error": {"code": "insufficient_quota"}}, "ML_PROVIDER_QUOTA_EXCEEDED"),
    ],
)
def test_permanent_provider_cause_is_not_hidden_by_transient_code(
    client, mock_router, interviewer_payload, status, body, expected
):
    mock_router.responses.append(httpx.Response(status, json=body))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.json()["error"]["code"] == expected
    assert response.json()["error"]["details"]["retryable"] is False
    assert len(mock_router.calls) == 1


def test_model_cannot_supply_its_own_meta(
    client, mock_router, interviewer_payload, interviewer_result
):
    bad = {**interviewer_result, "meta": {"prompt_tokens": 0}}
    mock_router.queue(bad, bad)
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ML_SCHEMA_ERROR"
    assert response.json()["meta"]["prompt_tokens"] == 240


def test_llm_models_preserve_full_validation_without_http_fields():
    from app.schemas.evaluator import EvaluatorLLMResult
    from app.schemas.interviewer import InterviewerLLMResult

    for model in (InterviewerLLMResult, EvaluatorLLMResult):
        schema = model.model_json_schema()
        encoded = json.dumps(schema)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for forbidden in ['"meta"', '"error"']:
            assert forbidden not in encoded
    # json_object affects only upstream generation, not our published validation.
    assert '"uniqueItems": true' in json.dumps(EvaluatorLLMResult.model_json_schema())
