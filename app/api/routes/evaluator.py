from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_openrouter_client
from app.clients.openrouter import OpenRouterClient
from app.schemas.evaluator import EvaluatorRequest, EvaluatorResponse
from app.services import evaluator as service

router = APIRouter(tags=["evaluator"])


@router.post("/evaluator", response_model=EvaluatorResponse)
async def evaluate(
    request: EvaluatorRequest,
    client: Annotated[OpenRouterClient, Depends(get_openrouter_client)],
) -> EvaluatorResponse:
    return await service.evaluate_answer(request, client)
