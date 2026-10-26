from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.evaluator import CriterionEvaluation, EvaluatorRequest, EvaluatorResponse
from app.services.evaluator import validate_evaluation

URL = "/api/v1/evaluator"


def test_evaluator_preserves_all_criteria(client, evaluator_payload):
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
        assert criterion["score"] == 0
        assert criterion["status"] == "absent"
        assert criterion["evidence"] == []
        assert criterion["comment"] == "Stub evaluator: real evaluation is not implemented yet."
        assert "explanation" not in criterion
    assert result["summary"] == "Stub evaluation: real LLM evaluation is not implemented yet."
    assert result["recommendations"] == []
    assert client.post(URL, json=evaluator_payload).json() == result


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
def rubric_response(client, evaluator_payload):
    return client.post(URL, json=evaluator_payload).json()


@pytest.mark.parametrize(("status", "score"), [("partial", 2), ("correct", 4), ("incorrect", 0)])
def test_domain_validation_accepts_valid_scores(evaluator_payload, rubric_response, status, score):
    rubric_response["criteria"][1].update(status=status, score=score)
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
