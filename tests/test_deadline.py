import asyncio
import json

import httpx

from app.clients.openrouter import OpenRouterClient
from app.schemas.interviewer import InterviewerRequest
from app.services.interviewer import generate_interviewer_turn
from app.settings import Settings


def test_deadline_cancels_inflight_transport_and_returns_safe_error(
    client, mock_router, interviewer_payload
):
    started = []
    cancelled = []

    async def respond(request):
        started.append(True)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    mock_router.client.settings.llm_request_timeout_seconds = 0.1
    mock_router.client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        response = client.post("/api/v1/interviewer", json=interviewer_payload)
        assert response.status_code == 504
        assert response.json() == {"detail": "ML provider request timed out"}
        assert started == [True]
        assert cancelled == [True]
    finally:
        asyncio.run(mock_router.client.http.aclose())


def test_repair_shares_the_original_deadline(interviewer_payload):
    async def scenario():
        calls = []

        async def respond(request):
            calls.append(True)
            await asyncio.sleep(0.15)
            content = (
                "invalid JSON"
                if len(calls) == 1
                else json.dumps({"action": "complete", "message": None})
            )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
            )

        settings = Settings(
            _env_file=None,
            openrouter_api_key="unit-test-placeholder",
            llm_request_timeout_seconds=0.25,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            from app.errors import ProviderTimeout

            try:
                await generate_interviewer_turn(
                    InterviewerRequest.model_validate(interviewer_payload),
                    OpenRouterClient(http, settings),
                )
            except ProviderTimeout:
                pass
            else:
                raise AssertionError("The repair attempt reset the deadline")
        assert len(calls) == 2

    asyncio.run(scenario())
