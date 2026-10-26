"""Two paid smoke tests; excluded by default, including when a key is present."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.evaluator import EvaluatorRequest, EvaluatorResponse
from app.schemas.interviewer import InterviewerResponse
from app.services.evaluator import validate_evaluation
from app.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def live_client():
    settings = Settings()
    if not settings.openrouter_api_key.get_secret_value().strip():
        pytest.skip("OPENROUTER_API_KEY is not configured")
    # Production path: two requests, at most four paid calls including semantic repairs.
    with TestClient(app) as test_client:
        router = app.state.openrouter_client
        yield test_client, router


def test_interviewer_live(live_client, interviewer_payload):
    test_client, router = live_client
    response = test_client.post("/api/v1/interviewer", json=interviewer_payload)
    assert response.status_code == 200, response.text
    InterviewerResponse.model_validate(response.json())
    assert router.settings.openrouter_model == "qwen/qwen3.5-27b"
    assert router.settings.openrouter_provider == "alibaba"


def test_evaluator_live(live_client, evaluator_payload):
    test_client, _ = live_client
    response = test_client.post("/api/v1/evaluator", json=evaluator_payload)
    assert response.status_code == 200, response.text
    result = EvaluatorResponse.model_validate(response.json())
    validate_evaluation(EvaluatorRequest.model_validate(evaluator_payload), result)
