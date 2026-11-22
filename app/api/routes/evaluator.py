from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_openrouter_client
from app.clients.openai import OpenAIClient
from app.clients.openrouter import OpenRouterClient
from app.schemas.evaluator import EvaluatorRequest, EvaluatorResponse
from app.schemas.meta import EvaluatorErrorResponse
from app.services import evaluator as service

router = APIRouter(tags=["evaluator"])


@router.post(
    "/evaluator",
    response_model=EvaluatorResponse,
    responses={code: {"model": EvaluatorErrorResponse} for code in (422, 500, 502, 503, 504)},
)
async def evaluate(
    request: EvaluatorRequest,
    http_request: Request,
    client: Annotated[OpenRouterClient | OpenAIClient, Depends(get_openrouter_client)],
) -> EvaluatorResponse:
    return await service.evaluate_answer(request, client, http_request.state.generation_context)
