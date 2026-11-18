from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.question_generation_http import (
    QuestionGenerationError,
    generation_error_response,
)
from app.schemas.question_generation import (
    QuestionGenerationErrorCode as Code,
)
from app.schemas.question_generation import (
    QuestionGenerationErrorResponse,
    QuestionGenerationRequest,
    QuestionGenerationResponse,
)
from app.services.question_generation import (
    QuestionGenerationService,
    create_question_generation_service,
)


class QuestionGenerationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return generation_error_response(request.scope, Code.INVALID_INPUT)
            except QuestionGenerationError as exc:
                return generation_error_response(request.scope, exc.code)

        return wrapped


router = APIRouter(tags=["question-generation"], route_class=QuestionGenerationRoute)


def get_question_generation_service(request: Request) -> QuestionGenerationService:
    if not hasattr(request.app.state, "question_generation_service"):
        client = request.app.state.openrouter_client
        request.app.state.question_generation_service = create_question_generation_service(
            client.http, client.settings
        )
    return request.app.state.question_generation_service


@router.post(
    "/question-generation",
    response_model=QuestionGenerationResponse,
    responses={
        code: {"model": QuestionGenerationErrorResponse}
        for code in (408, 409, 413, 415, 422, 500, 503)
    },
    description=(
        "Generate reviewed drafts with durable idempotency. Awaited internal operation; "
        "Core schedules its admin job and persists/publishes selected drafts. "
        "See docs/question-generation.md. Separate time/cost policy from MI-007."
    ),
)
async def generate_questions(
    payload: QuestionGenerationRequest,
    request: Request,
    service: Annotated[QuestionGenerationService, Depends(get_question_generation_service)],
) -> QuestionGenerationResponse:
    request.state.qgen_request_id = payload.request_id
    return await service.generate(payload)
