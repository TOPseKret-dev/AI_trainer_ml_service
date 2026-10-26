from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.evaluator import CriterionEvaluation, EvaluatorRequest, EvaluatorResponse
from app.services.evaluator import validate_evaluation

URL = "/api/v1/evaluator"


def test_evaluator_preserves_all_criteria(client, evaluator_payload, evaluator_result, mock_router):
    response = client.post(URL, json=evaluator_payload)
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"criteria", "summary", "recommendations"}
    assert len(result["criteria"]) == len(evaluator_payload["criteria"])
    assert [item["criterion_id"] for item in result["criteria"]] == [
        item["criterion_id"] for item in evaluator_payload["criteria"]
    ]
    for criterion in result["criteria"]:
        assert set(criterion) == {"criterion_id", "score", "status", "comment", "evidence"}
        assert "explanation" not in criterion
    assert result == evaluator_result
    assert len(mock_router.calls) == 1


def test_duplicate_criterion_ids(client, evaluator_payload):
    evaluator_payload["criteria"][1]["criterion_id"] = "grouping"
    assert client.post(URL, json=evaluator_payload).status_code == 422


def test_duplicate_anchor_scores_with_different_descriptions(client, evaluator_payload):
    evaluator_payload["criteria"][0]["anchors"].append(
        {"score": 1, "description": "Другое описание"}
    )
    assert client.post(URL, json=evaluator_payload).status_code == 422


def test_missing_anchor_zero(client, evaluator_payload):
    evaluator_payload["criteria"][0]["anchors"][0]["score"] = 2
    assert client.post(URL, json=evaluator_payload).status_code == 422


@pytest.mark.parametrize("score", [-1, 0.5, True, "1"])
def test_invalid_anchor_scores(client, evaluator_payload, score):
    evaluator_payload["criteria"][0]["anchors"][1]["score"] = score
    assert client.post(URL, json=evaluator_payload).status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("question", ""),
        ("target_level", "expert"),
        ("reference_answer", "  "),
        ("criteria", []),
        ("dialogue", []),
    ],
)
def test_invalid_request_fields(client, evaluator_payload, field, value):
    evaluator_payload[field] = value
    assert client.post(URL, json=evaluator_payload).status_code == 422


@pytest.mark.parametrize("location", ["root", "criterion", "anchor", "dialogue"])
def test_extra_fields_are_rejected(client, evaluator_payload, location):
    target = {
        "root": evaluator_payload,
        "criterion": evaluator_payload["criteria"][0],
        "anchor": evaluator_payload["criteria"][0]["anchors"][0],
        "dialogue": evaluator_payload["dialogue"][0],
    }[location]
    target["unexpected"] = True
    assert client.post(URL, json=evaluator_payload).status_code == 422


@pytest.mark.parametrize(
    ("status", "score", "evidence"),
    [
        ("absent", 1, []),
        ("absent", 0, ["Есть ответ"]),
        ("incorrect", 1, []),
        ("partial", 0, []),
        ("correct", 0, []),
        ("correct", 1, [""]),
        ("correct", 1, ["  "]),
        ("correct", 1, ["duplicate", "duplicate"]),
    ],
)
def test_output_structural_invariants(status, score, evidence):
    with pytest.raises(ValidationError):
        CriterionEvaluation(
            criterion_id="criterion",
            score=score,
            status=status,
            comment="comment",
            evidence=evidence,
        )


@pytest.fixture
def rubric_response(evaluator_result):
    return deepcopy(evaluator_result)


@pytest.mark.parametrize(("status", "score"), [("partial", 2), ("correct", 4), ("incorrect", 0)])
def test_domain_validation_accepts_valid_scores(evaluator_payload, rubric_response, status, score):
    rubric_response["criteria"][1].update(status=status, score=score)
    rubric_response["criteria"][1]["evidence"] = ["Группирует строки."]
    validate_evaluation(
        EvaluatorRequest.model_validate(evaluator_payload),
        EvaluatorResponse.model_validate(rubric_response),
    )


@pytest.mark.parametrize(("status", "score"), [("partial", 4), ("partial", 1), ("correct", 2)])
def test_domain_validation_rejects_invalid_scores(
    evaluator_payload, rubric_response, status, score
):
    rubric_response["criteria"][1].update(status=status, score=score)
    with pytest.raises(ValueError):
        validate_evaluation(
            EvaluatorRequest.model_validate(evaluator_payload),
            EvaluatorResponse.model_validate(rubric_response),
        )


@pytest.mark.parametrize("mutation", ["missing", "unknown", "extra"])
def test_domain_validation_rejects_incorrect_criterion_set(
    evaluator_payload, rubric_response, mutation
):
    if mutation == "missing":
        rubric_response["criteria"].pop()
    elif mutation == "unknown":
        rubric_response["criteria"][0]["criterion_id"] = "unknown"
    else:
        extra = deepcopy(rubric_response["criteria"][0])
        extra["criterion_id"] = "extra"
        rubric_response["criteria"].append(extra)
    with pytest.raises(ValueError):
        validate_evaluation(
            EvaluatorRequest.model_validate(evaluator_payload),
            EvaluatorResponse.model_validate(rubric_response),
        )


@pytest.mark.parametrize(("status", "score"), [("partial", 2), ("correct", 5)])
def test_arbitrary_anchors_endpoint(
    client, evaluator_payload, evaluator_result, mock_router, status, score
):
    evaluator_payload["criteria"][1]["anchors"][2]["score"] = 5
    evaluator_result["criteria"][1].update(
        status=status, score=score, evidence=["Группирует строки."]
    )
    response = client.post(URL, json=evaluator_payload)
    assert response.status_code == 200
    assert response.json()["criteria"][1]["score"] == score
    assert len(mock_router.calls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "non_anchor",
        "invalid_correct",
        "partial_impossible",
        "duplicate_id",
        "missing_id",
        "unknown_id",
        "ungrounded",
        "interviewer_evidence",
        "reference_evidence",
        "no_evidence",
    ],
)
def test_invalid_domain_result_gets_one_repair_then_502(
    client,
    evaluator_payload,
    evaluator_result,
    mock_router,
    mutation,
):
    evaluator_payload["criteria"][1]["anchors"][2]["score"] = 5
    result = deepcopy(evaluator_result)
    item = result["criteria"][1]
    item.update(status="partial", score=2, evidence=["Группирует строки."])
    if mutation == "non_anchor":
        item["score"] = 3
    elif mutation == "invalid_correct":
        item["status"] = "correct"
    elif mutation == "partial_impossible":
        evaluator_payload["criteria"][1]["anchors"] = [
            {"score": 0, "description": "Нет"},
            {"score": 3, "description": "Полный"},
        ]
        item["score"] = 3
    elif mutation == "duplicate_id":
        item["criterion_id"] = "grouping"
    elif mutation == "missing_id":
        result["criteria"].pop()
    elif mutation == "unknown_id":
        item["criterion_id"] = "unknown"
    elif mutation == "ungrounded":
        item["evidence"] = ["private-fabricated-evidence"]
    elif mutation == "interviewer_evidence":
        evaluator_payload["dialogue"].append(
            {"role": "interviewer", "content": "только интервьюер"}
        )
        item["evidence"] = ["только интервьюер"]
    elif mutation == "reference_evidence":
        item["evidence"] = [evaluator_payload["reference_answer"]]
    else:
        item["evidence"] = []
    mock_router.queue(result, result)
    response = client.post(URL, json=evaluator_payload)
    assert response.status_code == 502
    assert len(mock_router.calls) == 2
    correction = mock_router.calls[1]["messages"][2]
    assert correction["role"] == "system"
    assert "нарушил контракт" in correction["content"]
    assert "private-fabricated-evidence" not in correction["content"]


def test_repair_success_preserves_trust_boundary(
    client,
    evaluator_payload,
    evaluator_result,
    mock_router,
):
    invalid = deepcopy(evaluator_result)
    invalid["criteria"][0]["evidence"] = ["private-fabricated-evidence"]
    mock_router.queue(invalid, evaluator_result)
    response = client.post(URL, json=evaluator_payload)
    assert response.status_code == 200
    assert response.json() == evaluator_result
    assert len(mock_router.calls) == 2
    messages = mock_router.calls[1]["messages"]
    assert [message["role"] for message in messages] == ["system", "system", "system", "user"]
    assert "private-fabricated-evidence" not in messages[2]["content"]


def test_evidence_uses_all_candidate_messages(evaluator_payload, evaluator_result):
    evaluator_payload["dialogue"] = [
        {"role": "candidate", "content": "Группирует строки."},
        {"role": "interviewer", "content": "Продолжите."},
        {"role": "candidate", "content": "KV cache хранит вычисленные ключи и значения"},
    ]
    evaluator_result["criteria"][1].update(
        status="partial", score=2, evidence=["вычисленные ключи и значения"]
    )
    validate_evaluation(
        EvaluatorRequest.model_validate(evaluator_payload),
        EvaluatorResponse.model_validate(evaluator_result),
    )
    evaluator_result["criteria"][1]["evidence"] = ["ключи и значения attention для прошлых токенов"]
    with pytest.raises(ValueError, match="verbatim"):
        validate_evaluation(
            EvaluatorRequest.model_validate(evaluator_payload),
            EvaluatorResponse.model_validate(evaluator_result),
        )
