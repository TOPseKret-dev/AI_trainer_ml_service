import pytest
from pydantic import ValidationError

from app.schemas.interviewer import InterviewerResponse

URL = "/api/v1/interviewer"


def test_follow_up(client, interviewer_payload, interviewer_result, mock_router):
    response = client.post(URL, json=interviewer_payload)
    assert response.status_code == 200
    assert set(response.json()) == {"action", "message"}
    assert response.json()["action"] == "follow_up"
    assert response.json()["message"].strip()
    assert response.json() == interviewer_result
    assert len(mock_router.calls) == 1


def test_early_complete(client, interviewer_payload, interviewer_scenarios, mock_router):
    interviewer_payload["dialogue"][0]["content"] = interviewer_scenarios["complete"]
    mock_router.queue({"action": "complete", "message": None})
    response = client.post(URL, json=interviewer_payload)
    assert response.status_code == 200
    assert response.json() == {"action": "complete", "message": None}
    assert len(mock_router.calls) == 1


@pytest.mark.parametrize(("count", "maximum"), [(3, 3), (4, 3), (0, 0)])
def test_complete_at_or_above_limit(client, interviewer_payload, mock_router, count, maximum):
    interviewer_payload.update(follow_up_count=count, max_follow_ups=maximum)
    response = client.post(URL, json=interviewer_payload)
    assert response.status_code == 200
    assert response.json() == {"action": "complete", "message": None}
    assert mock_router.calls == []


@pytest.mark.parametrize(
    "result",
    [
        {"action": "complete", "message": "must not be repaired silently"},
        {"action": "follow_up", "message": "  "},
        {"action": "follow_up", "message": None},
        {"action": "other", "message": None},
        {"action": "complete", "message": None, "extra": "forbidden"},
        "not JSON",
    ],
)
def test_invalid_output_has_at_most_one_repair(client, interviewer_payload, mock_router, result):
    mock_router.queue(result, result)
    response = client.post(URL, json=interviewer_payload)
    assert response.status_code == 502
    assert len(mock_router.calls) == 2


def test_structural_repair_succeeds(client, interviewer_payload, mock_router, interviewer_result):
    mock_router.queue({"action": "complete", "message": "invalid"}, interviewer_result)
    response = client.post(URL, json=interviewer_payload)
    assert response.status_code == 200
    assert response.json() == interviewer_result
    assert len(mock_router.calls) == 2


def test_invalid_role(client, interviewer_payload):
    interviewer_payload["dialogue"][0]["role"] = "system"
    assert client.post(URL, json=interviewer_payload).status_code == 422


@pytest.mark.parametrize("location", ["root", "criterion", "dialogue"])
def test_extra_fields(client, interviewer_payload, location):
    target = {
        "root": interviewer_payload,
        "criterion": interviewer_payload["criteria"][0],
        "dialogue": interviewer_payload["dialogue"][0],
    }[location]
    target["unexpected"] = True
    assert client.post(URL, json=interviewer_payload).status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("question", ""),
        ("question", "  "),
        ("criteria", []),
        ("dialogue", []),
        ("follow_up_count", -1),
        ("max_follow_ups", -1),
        ("follow_up_count", True),
    ],
)
def test_invalid_input(client, interviewer_payload, field, value):
    interviewer_payload[field] = value
    assert client.post(URL, json=interviewer_payload).status_code == 422


@pytest.mark.parametrize("field", ["follow_up_count", "max_follow_ups"])
def test_counts_are_required(client, interviewer_payload, field):
    del interviewer_payload[field]
    assert client.post(URL, json=interviewer_payload).status_code == 422


@pytest.mark.parametrize(
    ("action", "message"),
    [("follow_up", None), ("follow_up", ""), ("follow_up", "  "), ("complete", "text")],
)
def test_response_action_message_invariant(action, message):
    with pytest.raises(ValidationError):
        InterviewerResponse(action=action, message=message)
