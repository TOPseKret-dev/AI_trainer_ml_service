import json
import re
from copy import deepcopy

import httpx
import pytest
from conftest import contract_validator
from jsonschema import ValidationError as SchemaValidationError

from app.contracts import ROOT, schemas
from app.errors import ERROR_CATALOG, MLErrorCode, MLServiceError
from app.generation import GenerationContext
from app.schemas.evaluator import EvaluatorLLMResult, EvaluatorRequest
from app.schemas.interviewer import InterviewerLLMResult, InterviewerRequest
from app.schemas.meta import MLResponseMeta


@pytest.mark.parametrize("name,schema", list(schemas().items()))
def test_committed_schemas_are_generated_from_runtime_models(name, schema):
    assert json.loads((ROOT / name).read_text()) == schema
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    if "output" in name:
        assert len(schema["oneOf"]) == 2


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
def test_input_and_http_output_contracts(client, interviewer_payload, evaluator_payload, operation):
    payload = interviewer_payload if operation == "interviewer" else evaluator_payload
    contract_validator(operation, "input").validate(payload)
    response = client.post(f"/api/v1/{operation}", json=payload)
    assert response.status_code == 200
    meta = MLResponseMeta.model_validate(response.json()["meta"])
    assert meta.provider_attempts == 1
    assert meta.prompt_tokens == 120 and meta.completion_tokens == 30


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
@pytest.mark.parametrize("mutation", ["missing", "blank", "type", "nested", "role", "duplicate"])
def test_invalid_input_is_safe_and_never_calls_provider(
    client, mock_router, interviewer_payload, evaluator_payload, operation, mutation
):
    data = interviewer_payload if operation == "interviewer" else evaluator_payload
    marker = "PRIVATE-INVALID-INPUT"
    data["question"] = marker
    if mutation == "missing":
        del data["question"]
    elif mutation == "blank":
        data["question"] = " \n\t"
    elif mutation == "type":
        data["criteria"][0]["criterion_id"] = False
    elif mutation == "nested":
        data["criteria"][0]["weight"] = 1.0
    elif mutation == "role":
        data["dialogue"][0]["role"] = "system"
    else:
        data["criteria"].append(deepcopy(data["criteria"][0]))
    response = client.post(f"/api/v1/{operation}", json=data)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ML_INVALID_INPUT"
    assert response.json()["error"]["details"]["retryable"] is False
    assert response.json()["meta"]["provider_attempts"] == 0
    assert mock_router.calls == []
    assert marker not in response.text


@pytest.mark.parametrize("body", ['{"question":', "null", "[]", "true"])
def test_malformed_json_is_v2_error(client, mock_router, body):
    response = client.post(
        "/api/v1/interviewer", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ML_INVALID_INPUT"
    assert mock_router.calls == []


@pytest.mark.parametrize("anchors", [[0], [1, 2], [0, 0], [0, 2], [0, 1, 3]])
def test_incomplete_or_invalid_rubric_is_rejected_before_provider(
    client, mock_router, evaluator_payload, anchors
):
    evaluator_payload["criteria"][0]["anchors"] = [
        {"score": n, "description": "anchor"} for n in anchors
    ]
    response = client.post("/api/v1/evaluator", json=evaluator_payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ML_INVALID_INPUT"
    assert mock_router.calls == []


def test_no_candidate_has_its_own_error(client, mock_router, evaluator_payload):
    evaluator_payload["dialogue"] = [{"role": "interviewer", "content": "Расскажите о GROUP BY"}]
    response = client.post("/api/v1/evaluator", json=evaluator_payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ML_NOTHING_TO_EVALUATE"
    assert response.json()["meta"]["provider_attempts"] == 0
    assert mock_router.calls == []


@pytest.mark.parametrize(
    "maximum,status,score",
    [
        (1, "correct", 1),
        (1, "incorrect", 0),
        (2, "partial", 1),
        (2, "correct", 2),
        (3, "partial", 2),
        (3, "correct", 3),
    ],
)
def test_dynamic_rubric_scales(client, evaluator_payload, evaluator_result, maximum, status, score):
    evaluator_payload["criteria"][0]["anchors"] = [
        {"score": n, "description": f"anchor {n}"} for n in range(maximum + 1)
    ]
    evaluator_result["criteria"][0].update(status=status, score=score)
    response = client.post("/api/v1/evaluator", json=evaluator_payload)
    assert response.status_code == 200
    assert response.json()["criteria"][0]["score"] == score


@pytest.mark.parametrize("field", ["comment", "summary", "recommendations", "evidence"])
def test_invalid_output_strings_get_repaired_then_fail(
    client, mock_router, evaluator_payload, evaluator_result, field
):
    bad = deepcopy(evaluator_result)
    if field == "comment":
        bad["criteria"][0]["comment"] = " "
    elif field == "summary":
        bad["summary"] = " "
    elif field == "recommendations":
        bad["recommendations"] = [" "]
    else:
        bad["criteria"][0]["evidence"] *= 2
    mock_router.queue(bad, bad)
    response = client.post("/api/v1/evaluator", json=evaluator_payload)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ML_SCHEMA_ERROR"
    assert response.json()["meta"]["provider_attempts"] == 2


def test_empty_recommendations_are_valid(client, evaluator_payload, evaluator_result):
    evaluator_result["recommendations"] = []
    assert client.post("/api/v1/evaluator", json=evaluator_payload).json()["recommendations"] == []


@pytest.mark.parametrize(
    "code,status,retryable",
    [
        ("ML_INVALID_INPUT", 422, False),
        ("ML_NOTHING_TO_EVALUATE", 422, False),
        ("ML_PROVIDER_NOT_CONFIGURED", 503, False),
        ("ML_PROVIDER_AUTH_ERROR", 503, False),
        ("ML_PROVIDER_QUOTA_EXCEEDED", 503, False),
        ("ML_PROVIDER_CONFIG_ERROR", 503, False),
        ("ML_RATE_LIMITED", 503, True),
        ("ML_UNAVAILABLE", 503, True),
        ("ML_QUEUE_FULL", 503, True),
        ("ML_TIMEOUT", 504, True),
        ("ML_QUEUE_TIMEOUT", 504, True),
        ("ML_EMPTY_RESPONSE", 502, False),
        ("ML_OUTPUT_TRUNCATED", 502, False),
        ("ML_SCHEMA_ERROR", 502, False),
        ("ML_INVALID_RESPONSE", 502, False),
        ("ML_POLICY_REFUSAL", 422, False),
        ("ML_INTERNAL_ERROR", 500, False),
    ],
)
def test_every_catalogue_code_has_a_safe_http_interface(
    client, monkeypatch, interviewer_payload, evaluator_payload, code, status, retryable
):
    from app.services import evaluator, interviewer

    assert len(ERROR_CATALOG) == 17
    operation = "evaluator" if code == MLErrorCode.NOTHING_TO_EVALUATE else "interviewer"

    async def fail(*args, **kwargs):
        raise MLServiceError(MLErrorCode(code))

    if operation == "evaluator":
        monkeypatch.setattr(evaluator, "evaluate_answer", fail)
        payload = evaluator_payload
    else:
        monkeypatch.setattr(interviewer, "generate_interviewer_turn", fail)
        payload = interviewer_payload
    response = client.post(f"/api/v1/{operation}", json=payload)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"]["operation"] == operation
    assert response.json()["error"]["details"]["retryable"] is retryable


def test_unexpected_exception_is_internal_and_never_leaks(client, mock_router, interviewer_payload):
    mock_router.responses.append(RuntimeError("PRIVATE-EXCEPTION-AND-SECRET"))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "ML_INTERNAL_ERROR"
    assert response.json()["error"]["details"]["retryable"] is False
    assert "PRIVATE" not in response.text
    assert response.json()["meta"]["provider_attempts"] == 1


def test_broken_metadata_never_hides_provider_error(
    client, mock_router, interviewer_payload, monkeypatch
):
    def broken(self):
        raise RuntimeError("PRIVATE-METADATA-ERROR")

    monkeypatch.setattr(GenerationContext, "meta", broken)
    mock_router.responses.append(httpx.Response(401))
    response = client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ML_PROVIDER_AUTH_ERROR"
    assert "meta" not in response.json()
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
def test_negative_output_contracts(client, interviewer_payload, evaluator_payload, operation):
    payload = interviewer_payload if operation == "interviewer" else evaluator_payload
    good = client.post(f"/api/v1/{operation}", json=payload).json()
    invalid = []
    without_meta = deepcopy(good)
    del without_meta["meta"]
    invalid.append(without_meta)
    invalid.extend(
        [
            {**good, "error": {"code": "ML_INTERNAL_ERROR", "message": "error"}},
            {**good, "unexpected": True},
            {"error": {"code": "ML_NEW_UNKNOWN_CODE", "message": "error"}},
            {
                "error": {
                    "code": "ML_INTERNAL_ERROR",
                    "message": "error",
                    "details": {"raw": "secret"},
                }
            },
            {"error": {"code": "ML_INTERNAL_ERROR", "message": "error"}, "meta": None},
        ]
    )
    for field, value in [
        ("provider_attempts", 3),
        ("provider_attempts", True),
        ("duration_ms", -1),
        ("prompt_tokens", "123"),
    ]:
        bad = deepcopy(good)
        bad["meta"][field] = value
        invalid.append(bad)
    if operation == "interviewer":
        invalid.append({**good, "action": "complete", "message": "wrong"})
    else:
        bad = deepcopy(good)
        bad["criteria"][0].update(status="absent", score=1)
        invalid.append(bad)
    for bad in invalid:
        with pytest.raises(SchemaValidationError):
            contract_validator(operation).validate(bad)


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
def test_llm_schema_never_contains_transport_metadata(operation):
    model = InterviewerLLMResult if operation == "interviewer" else EvaluatorLLMResult
    schema = json.dumps(model.model_json_schema())
    for forbidden in ['"meta"', '"error"', '"prompt_tokens"', '"duration_ms"']:
        assert forbidden not in schema


@pytest.mark.parametrize("operation", ["interviewer", "evaluator"])
def test_openapi_declares_v2_errors_and_success_meta(client, operation):
    schema = client.get("/openapi.json").json()
    responses = schema["paths"][f"/api/v1/{operation}"]["post"]["responses"]
    assert set(responses) == {"200", "422", "500", "502", "503", "504"}
    for status in ("422", "500", "502", "503", "504"):
        ref = responses[status]["content"]["application/json"]["schema"]["$ref"]
        assert "MLErrorResponse" in ref
        assert "HTTPValidationError" not in ref
    ref = responses["200"]["content"]["application/json"]["schema"]["$ref"]
    definition = schema["components"]["schemas"][ref.rsplit("/", 1)[1]]
    assert "meta" in definition["required"]


@pytest.mark.parametrize("operation", ["interviewer", "evaluator", "rate_limited"])
def test_record_real_http_examples(
    client, mock_router, interviewer_payload, evaluator_payload, record_property, operation
):
    if operation == "rate_limited":
        mock_router.responses.append(httpx.Response(429, headers={"Retry-After": "10"}))
    path = "/api/v1/evaluator" if operation == "evaluator" else "/api/v1/interviewer"
    payload = evaluator_payload if operation == "evaluator" else interviewer_payload
    response = client.post(path, json=payload)
    assert response.status_code == (503 if operation == "rate_limited" else 200)
    record_property(
        "http_example",
        json.dumps(
            {
                "operation": operation,
                "method": "POST",
                "path": path,
                "request": payload,
                "status": response.status_code,
                "response": response.json(),
            },
            ensure_ascii=False,
        ),
    )


def test_documented_json_examples_match_v2_models_and_schemas():
    from app.schemas.question_bank import QuestionBankGenerationRequest
    from app.services.evaluator import validate_evaluation

    text = (ROOT / "docs/api.md").read_text()
    pattern = r"curl -X POST http://127\.0\.0\.1:8001(/api/v1/[^\s]+).*?<<'JSON'\n(.*?)\nJSON"
    examples = {}
    models = {
        "interviewer": InterviewerRequest,
        "evaluator": EvaluatorRequest,
        "question-bank/generate": QuestionBankGenerationRequest,
    }
    for path, body in re.findall(pattern, text, re.S):
        operation = path.removeprefix("/api/v1/")
        examples[operation] = models[operation].model_validate_json(body)
        if operation in {"interviewer", "evaluator"}:
            contract_validator(operation, "input").validate(json.loads(body))
    assert len(examples) == 3
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    assert len(blocks) == 3
    for block in blocks:
        data = json.loads(block)
        operation = "interviewer" if "action" in data else "evaluator"
        contract_validator(operation).validate(data)
        if "criteria" in data:
            result = EvaluatorLLMResult.model_validate(
                {k: v for k, v in data.items() if k != "meta"}
            )
            validate_evaluation(examples["evaluator"], result)


def test_documentation_relative_links_resolve():
    for file in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", file.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            assert (file.parent / target.split("#", 1)[0]).exists(), (file, target)
