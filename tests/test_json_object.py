import pytest

from app.settings import Settings


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
@pytest.mark.parametrize("kind", ["empty-array", "wrapped-object", "null"])
@pytest.mark.parametrize("repair_succeeds", [False, True])
def test_json_mode_preserves_result_validation_and_single_repair(
    client,
    mock_router,
    interviewer_payload,
    evaluator_payload,
    interviewer_result,
    evaluator_result,
    operation,
    kind,
    repair_succeeds,
):
    """Reproduce Alibaba's HTTP-200 arrays without accepting or unwrapping them."""
    mock_router.client.settings = Settings(
        _env_file=None,
        openrouter_api_key="unit-test-placeholder",
        openrouter_model="qwen/qwen3.5-27b",
        openrouter_provider="alibaba",
    )
    payload = interviewer_payload if operation == "interviewer" else evaluator_payload
    valid = interviewer_result if operation == "interviewer" else evaluator_result
    invalid = {"empty-array": [], "wrapped-object": [valid], "null": None}[kind]
    mock_router.queue(invalid, valid if repair_succeeds else invalid)

    response = client.post(f"/api/v1/{operation}", json=payload)

    assert response.status_code == (200 if repair_succeeds else 502)
    assert len(mock_router.calls) == 2
    assert all(call["response_format"] == {"type": "json_object"} for call in mock_router.calls)
    result = response.json()
    assert result["meta"]["provider_attempts"] == 2
    if repair_succeeds:
        assert all(result[key] == value for key, value in valid.items())
    else:
        assert result["error"]["code"] == "ML_SCHEMA_ERROR"


@pytest.mark.parametrize(
    "result",
    ['{"action":"complete","message":null', '{"action":"follow_up","message":"Что произойдёт?"'],
)
def test_missing_final_brace_is_recovered_without_a_second_call(
    client, mock_router, interviewer_payload, result
):
    mock_router.queue(result)
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 200
    assert response.json()["meta"]["provider_attempts"] == 1
    assert len(mock_router.calls) == 1


@pytest.mark.parametrize(
    "result",
    [
        '{"action":"follow_up","message":"незавершённая',
        '{"action":"complete","message":"нельзя"',
        "null",
        "[]",
    ],
)
def test_incomplete_or_invalid_content_requires_provider_repair(
    client, mock_router, interviewer_payload, interviewer_result, result
):
    mock_router.queue(result, interviewer_result)
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 200
    assert response.json()["meta"]["provider_attempts"] == 2
    assert len(mock_router.calls) == 2
    correction = mock_router.calls[1]["messages"][3]["content"]
    assert "ML_SCHEMA_ERROR" in correction
    assert "незавершённая" not in correction
    assert "нельзя" not in correction
