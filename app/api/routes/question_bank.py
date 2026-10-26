from fastapi import APIRouter

from app.schemas.question_bank import (
    QuestionBankGenerationRequest,
    QuestionBankGenerationResponse,
)
from app.services import question_bank as service

router = APIRouter(tags=["question-bank"])


# PROVISIONAL CONTRACT: TODO replace after agreement between backend ↔ ML.
@router.post(
    "/question-bank/generate",
    response_model=QuestionBankGenerationResponse,
    description="PROVISIONAL CONTRACT. Generates drafts only; nothing is stored or published.",
)
async def generate_questions(
    request: QuestionBankGenerationRequest,
) -> QuestionBankGenerationResponse:
    return await service.generate_question_bank(request)
