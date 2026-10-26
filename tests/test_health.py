import logging


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ml-service"}


def test_openapi_has_all_endpoints_and_response_contracts(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert set(schema["paths"]) == {
        "/health",
        "/api/v1/transcriptions",
        "/api/v1/interviewer",
        "/api/v1/evaluator",
        "/api/v1/question-bank/generate",
    }
    for path, operations in schema["paths"].items():
        method = "get" if path == "/health" else "post"
        assert "schema" in operations[method]["responses"]["200"]["content"]["application/json"]
    evaluation_schema = schema["components"]["schemas"]["CriterionEvaluation"]
    assert "comment" in evaluation_schema["properties"]
    assert "explanation" not in evaluation_schema["properties"]
    assert evaluation_schema["additionalProperties"] is False


def test_documentation_pages(client):
    for path in ("/docs", "/redoc"):
        assert client.get(path).status_code == 200


def test_logs_show_outcome_duration_without_request_content(client, interviewer_payload, caplog):
    secret_marker = "sensitive-candidate-content-for-log-test"
    interviewer_payload["dialogue"][0]["content"] = secret_marker
    with caplog.at_level(logging.INFO):
        client.post("/api/v1/interviewer", json=interviewer_payload)
        interviewer_payload["criteria"] = []
        client.post("/api/v1/interviewer", json=interviewer_payload)
    application_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(("ml_service", "app.services"))
    )
    assert secret_marker not in application_logs
    assert "function=generate_interviewer_turn" in application_logs
    assert "endpoint=interviewer_turn" in application_logs
    assert "outcome=success status=200 duration_ms=" in application_logs
    assert "outcome=failure status=422 duration_ms=" in application_logs
