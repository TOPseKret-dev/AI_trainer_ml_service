from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_openrouter_client
from app.clients.openrouter import OpenRouterClient
from app.schemas.interviewer import InterviewerRequest, InterviewerResponse
from app.schemas.meta import InterviewerErrorResponse
from app.services import interviewer as service

router = APIRouter(tags=["interviewer"])


@router.post(
    "/interviewer",
    response_model=InterviewerResponse,
    responses={code: {"model": InterviewerErrorResponse} for code in (422, 500, 502, 503, 504)},
)
async def interviewer_turn(
    request: InterviewerRequest,
    http_request: Request,
    client: Annotated[OpenRouterClient, Depends(get_openrouter_client)],
) -> InterviewerResponse:
    return await service.generate_interviewer_turn(
        request, client, http_request.state.generation_context
    )
