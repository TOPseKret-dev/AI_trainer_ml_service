import logging

from app.clients.openai import OpenAIClient
from app.clients.openrouter import OpenRouterClient
from app.generation import GenerationContext
from app.prompts.interviewer import INTERVIEWER_PROMPT_VERSION, INTERVIEWER_SYSTEM_PROMPT
from app.schemas.interviewer import InterviewerLLMResult, InterviewerRequest, InterviewerResponse
from app.services.structured import build_messages, generate_structured

logger = logging.getLogger(__name__)


async def generate_interviewer_turn(
    request: InterviewerRequest,
    client: OpenRouterClient | OpenAIClient,
    context: GenerationContext | None = None,
) -> InterviewerResponse:
    context = context or GenerationContext("interviewer")
    context.configure_deadline(client.settings.llm_request_timeout_seconds, request.deadline_at)
    if request.follow_up_count >= request.max_follow_ups:
        result = InterviewerLLMResult(action="complete", message=None)
    else:
        result = await generate_structured(
            client=client,
            context=context,
            messages=build_messages(
                INTERVIEWER_SYSTEM_PROMPT,
                request.model_dump_json(exclude={"dialogue", "deadline_at"}),
                request.dialogue,
            ),
            response_model=InterviewerLLMResult,
            service="interviewer",
            prompt_version=INTERVIEWER_PROMPT_VERSION,
            temperature=0.2,
        )
    context.remaining()
    logger.info("function=generate_interviewer_turn outcome=success action=%s", result.action)
    return InterviewerResponse(**result.model_dump(), meta=context.meta())
