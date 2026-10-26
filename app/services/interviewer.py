import logging

from app.schemas.interviewer import InterviewerRequest, InterviewerResponse

logger = logging.getLogger(__name__)


async def generate_interviewer_turn(request: InterviewerRequest) -> InterviewerResponse:
    # TODO replace this deterministic stub with inference; preserve the HTTP contract.
    if request.follow_up_count >= request.max_follow_ups:
        result = InterviewerResponse(action="complete", message=None)
    else:
        result = InterviewerResponse(
            action="follow_up", message="Можете подробнее обосновать это утверждение?"
        )
    logger.info("function=generate_interviewer_turn outcome=success action=%s", result.action)
    return result
