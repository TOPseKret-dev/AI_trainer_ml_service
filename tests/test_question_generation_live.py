"""One explicit paid HTTP canary, never selected by the default test suite."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.question_generation import QuestionGenerationResponse
from app.settings import Settings

pytestmark = pytest.mark.integration


def test_question_generation_live(tmp_path, monkeypatch):
    if not Settings().openrouter_api_key.get_secret_value().strip():
        pytest.skip("OPENROUTER_API_KEY is not configured")
    monkeypatch.setenv("QUESTIONGEN_STATE_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("QUESTIONGEN_POLICY__MAX_COST_USD", "0.35")
    monkeypatch.setenv("QUESTIONGEN_POLICY__DEADLINE_SECONDS", "180")
    payload = {
        "request_id": str(uuid4()),
        "specialization": "analytics",
        "area": "sql",
        "grade": "Middle",
        "count": 1,
        "comment": "Один узкий вопрос о COUNT и NULL в PostgreSQL, без сложной схемы таблиц.",
        "existing_questions": [],
        "bank_context_complete": True,
        "materials": [],
    }
    with TestClient(app) as http:
        response = http.post("/api/v1/question-generation", json=payload)
        assert response.status_code == 200, response.text
        result = QuestionGenerationResponse.model_validate(response.json())
        assert result.status == "completed", response.text
        assert len(result.questions) == 1
        assert result.questions[0].status == "draft"
        assert result.questions[0].generation.quality_status == "passed"
        assert result.metrics.calls >= 3  # plan + write + independent review
        assert result.metrics.cost_usd > 0

        async def forbidden(*_):
            raise AssertionError("Idempotent replay must not call OpenRouter")

        provider = app.state.question_generation_service.generator.provider
        monkeypatch.setattr(provider, "complete", forbidden)
        cached = http.post("/api/v1/question-generation", json=payload)
        assert cached.status_code == 200
        assert cached.json() == response.json()

    directory = Path("artifacts/questiongen-integration-20261119")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "http-example.json").write_text(
        json.dumps({"request": payload, "response": response.json()}, ensure_ascii=False, indent=2)
        + "\n"
    )
