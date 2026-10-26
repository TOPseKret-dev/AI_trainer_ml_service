from fastapi import APIRouter

from app.schemas.interviewer import InterviewerRequest, InterviewerResponse
from app.services import interviewer as service

router = APIRouter(tags=["interviewer"])


@router.post("/interviewer", response_model=InterviewerResponse)
async def interviewer_turn(request: InterviewerRequest) -> InterviewerResponse:
    return await service.generate_interviewer_turn(request)
