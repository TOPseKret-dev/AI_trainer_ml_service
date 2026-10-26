from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_openrouter_client
from app.clients.openrouter import OpenRouterClient
from app.schemas.interviewer import InterviewerRequest, InterviewerResponse
from app.services import interviewer as service

router = APIRouter(tags=["interviewer"])


@router.post("/interviewer", response_model=InterviewerResponse)
async def interviewer_turn(
    request: InterviewerRequest,
    client: Annotated[OpenRouterClient, Depends(get_openrouter_client)],
) -> InterviewerResponse:
    return await service.generate_interviewer_turn(request, client)
