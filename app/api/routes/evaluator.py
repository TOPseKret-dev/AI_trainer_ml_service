from fastapi import APIRouter

from app.schemas.evaluator import EvaluatorRequest, EvaluatorResponse
from app.services import evaluator as service

router = APIRouter(tags=["evaluator"])


@router.post("/evaluator", response_model=EvaluatorResponse)
async def evaluate(request: EvaluatorRequest) -> EvaluatorResponse:
    return await service.evaluate_answer(request)
