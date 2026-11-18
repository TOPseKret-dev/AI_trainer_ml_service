import pytest


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
    payload = interviewer_payload if operation == "interviewer" else evaluator_payload
    valid = interviewer_result if operation == "interviewer" else evaluator_result
    invalid = {"empty-array": [], "wrapped-object": [valid], "null": None}[kind]
    mock_router.queue(invalid, valid if repair_succeeds else invalid)

    response = client.post(f"/api/v1/{operation}", json=payload)

    assert response.status_code == (200 if repair_succeeds else 502)
    assert len(mock_router.calls) == 2
    assert all(call["response_format"] == {"type": "json_object"} for call in mock_router.calls)
    if repair_succeeds:
        result = response.json()
        assert all(result[key] == value for key, value in valid.items())
