import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def interviewer_payload():
    return {
        "question": "Что делает GROUP BY?",
        "criteria": [{"criterion_id": "grouping", "description": "Объясняет группировку"}],
        "dialogue": [{"role": "candidate", "content": "Группирует строки."}],
        "follow_up_count": 0,
        "max_follow_ups": 3,
    }


@pytest.fixture
def evaluator_payload():
    return {
        "question": "Что делает GROUP BY?",
        "target_level": "middle",
        "reference_answer": "Группирует строки для вычисления агрегатов.",
        "criteria": [
            {
                "criterion_id": "grouping",
                "description": "Объясняет группировку",
                "anchors": [
                    {"score": 0, "description": "Нет объяснения"},
                    {"score": 1, "description": "Корректное объяснение"},
                ],
            },
            {
                "criterion_id": "aggregation",
                "description": "Объясняет агрегаты",
                "anchors": [
                    {"score": 0, "description": "Нет объяснения"},
                    {"score": 2, "description": "Частичное объяснение"},
                    {"score": 4, "description": "Полное объяснение"},
                ],
            },
        ],
        "dialogue": [{"role": "candidate", "content": "Группирует строки."}],
    }


@pytest.fixture
def question_bank_payload():
    return {
        "specialization": "Аналитика",
        "topic": "SQL",
        "min_grade": "junior",
        "difficulty": "medium",
        "count": 2,
    }
