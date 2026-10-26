import logging

from app.clients.openrouter import OpenRouterClient
from app.prompts.interviewer import INTERVIEWER_PROMPT_VERSION, INTERVIEWER_SYSTEM_PROMPT
from app.schemas.interviewer import InterviewerRequest, InterviewerResponse
from app.services.structured import build_messages, generate_structured

logger = logging.getLogger(__name__)


async def generate_interviewer_turn(
    request: InterviewerRequest, client: OpenRouterClient
) -> InterviewerResponse:
    if request.follow_up_count >= request.max_follow_ups:
        result = InterviewerResponse(action="complete", message=None)
    else:
        result = await generate_structured(
            client=client,
            messages=build_messages(
                INTERVIEWER_SYSTEM_PROMPT,
                request.model_dump_json(exclude={"dialogue"}),
                request.dialogue,
            ),
            response_model=InterviewerResponse,
            service="interviewer",
            prompt_version=INTERVIEWER_PROMPT_VERSION,
            temperature=0.2,
        )
    logger.info("function=generate_interviewer_turn outcome=success action=%s", result.action)
    return result
