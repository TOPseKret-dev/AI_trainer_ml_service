from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.schemas.transcription import TranscriptionResponse
from app.services.transcription import AsrError, AsrService


class AsrRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "ASR_INVALID_INPUT",
                            "message": "Invalid ASR input",
                            "details": {"retryable": False},
                        }
                    },
                )

        return wrapped


router = APIRouter(tags=["transcription"], route_class=AsrRoute)


def get_asr_service(request: Request) -> AsrService:
    if not hasattr(request.app.state, "asr_service"):
        request.app.state.asr_service = AsrService()
    return request.app.state.asr_service


@router.post(
    "/transcriptions",
    response_model=TranscriptionResponse,
    description="Internal Core to ML ASR; see docs/asr.md",
)
async def create_transcription(
    file: Annotated[UploadFile, File()],
    attempt_id: Annotated[UUID, Form()],
    question_text: Annotated[str, Form(min_length=1, max_length=10000)],
    specialization_id: Annotated[UUID, Form()],
    deadline_at: Annotated[datetime, Form()],
    service: Annotated[AsrService, Depends(get_asr_service)],
):
    try:
        if deadline_at.tzinfo is None:
            raise AsrError("ASR_INVALID_INPUT", 422)
        if (file.content_type or "").split(";")[0].strip().lower() != "audio/webm":
            raise AsrError("AUDIO_FORMAT_UNSUPPORTED", 415)
        data = await file.read(25000001)
        if len(data) > 25000000:
            raise AsrError("PAYLOAD_TOO_LARGE", 413)
        if not data:
            raise AsrError("AUDIO_EMPTY", 422)
        await file.close()
        return await service.transcribe(data, attempt_id, question_text, deadline_at)
    except AsrError as exc:
        return JSONResponse(
            status_code=exc.status,
            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.code,
                    "details": {"attempt_id": str(attempt_id), "retryable": exc.retryable},
                }
            },
        )
    finally:
        await file.close()
