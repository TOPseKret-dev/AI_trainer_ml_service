from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.question_bank import QuestionBankGenerationResponse

URL = "/api/v1/question-bank/generate"


@pytest.mark.parametrize(
    ("minimum", "expected_grades"),
    [
        ("junior", ["junior", "middle", "senior"]),
        ("middle", ["middle", "senior"]),
        ("senior", ["senior"]),
    ],
)
def test_generation_grade_coverage(client, question_bank_payload, minimum, expected_grades):
    question_bank_payload["min_grade"] = minimum
    response = client.post(URL, json=question_bank_payload)
    assert response.status_code == 200
    questions = response.json()["questions"]
    assert len(questions) == question_bank_payload["count"]
    for index, draft in enumerate(questions, start=1):
        assert draft["question_text"].startswith(f"[STUB {index}]")
        assert [answer["grade"] for answer in draft["reference_answers"]] == expected_grades
        assert all(answer["reference_answer"] for answer in draft["reference_answers"])
        ids = [criterion["criterion_id"] for criterion in draft["criteria"]]
        assert len(ids) == len(set(ids))
        for criterion in draft["criteria"]:
            assert [config["grade"] for config in criterion["grade_configs"]] == expected_grades
            for config in criterion["grade_configs"]:
                scores = [anchor["score"] for anchor in config["anchors"]]
                assert 0 in scores
                assert any(score > 0 for score in scores)
                assert len(scores) == len(set(scores))
                assert config["weight"] > 0
                assert config["max_score"] == max(scores)
                assert config["expected_level"]
    assert client.post(URL, json=question_bank_payload).json() == response.json()


def test_count_default(client, question_bank_payload):
    del question_bank_payload["count"]
    response = client.post(URL, json=question_bank_payload)
    assert response.status_code == 200
    assert len(response.json()["questions"]) == 10


def test_count_maximum(client, question_bank_payload):
    question_bank_payload["count"] = 50
    response = client.post(URL, json=question_bank_payload)
    assert response.status_code == 200
    assert len(response.json()["questions"]) == 50


@pytest.mark.parametrize("count", [0, -1, 51, 1.5, True])
def test_invalid_count(client, question_bank_payload, count):
    question_bank_payload["count"] = count
    assert client.post(URL, json=question_bank_payload).status_code == 422


def test_extra_field(client, question_bank_payload):
    question_bank_payload["publish"] = True
    assert client.post(URL, json=question_bank_payload).status_code == 422


def test_difficulty_is_not_an_enum(client, question_bank_payload):
    question_bank_payload["difficulty"] = "custom-unagreed-scale"
    assert client.post(URL, json=question_bank_payload).status_code == 200


@pytest.mark.parametrize("field", ["specialization", "topic", "difficulty"])
def test_blank_fields(client, question_bank_payload, field):
    question_bank_payload[field] = "  "
    assert client.post(URL, json=question_bank_payload).status_code == 422


@pytest.mark.parametrize(
    "mutation",
    [
        "weight",
        "max_score",
        "missing_zero",
        "duplicate_criterion",
        "duplicate_grade",
        "missing_grade",
    ],
)
def test_draft_response_invariants(client, question_bank_payload, mutation):
    result = client.post(URL, json=question_bank_payload).json()
    draft = result["questions"][0]
    criterion = draft["criteria"][0]
    config = criterion["grade_configs"][0]
    if mutation == "weight":
        config["weight"] = 0
    elif mutation == "max_score":
        config["max_score"] = 9
    elif mutation == "missing_zero":
        config["anchors"].pop(0)
    elif mutation == "duplicate_criterion":
        draft["criteria"].append(deepcopy(criterion))
    elif mutation == "duplicate_grade":
        criterion["grade_configs"].append(deepcopy(config))
    else:
        draft["reference_answers"].pop()
    with pytest.raises(ValidationError):
        QuestionBankGenerationResponse.model_validate(result)
