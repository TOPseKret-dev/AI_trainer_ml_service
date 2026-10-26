from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.schemas.transcription import TranscriptionResponse
from app.services import transcription as service

router = APIRouter(tags=["transcription"])


# PROVISIONAL CONTRACT: TODO agree multipart fields and response with backend ↔ ML.
@router.post(
    "/transcriptions",
    response_model=TranscriptionResponse,
    description="PROVISIONAL CONTRACT. Returns a deterministic transcription stub.",
    responses={400: {"description": "Empty file or non-audio Content-Type"}},
)
async def create_transcription(
    file: Annotated[UploadFile, File()],
    language: Annotated[str, Form(min_length=1, pattern=r"\S")] = "ru",
) -> TranscriptionResponse:
    if file.content_type is not None and not file.content_type.lower().startswith("audio/"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File Content-Type must start with audio/")
    # Read one byte to detect emptiness without loading the audio into memory or imposing a limit.
    if not await file.read(1):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Audio file must not be empty")
    await file.seek(0)
    return await service.transcribe_audio(file.file, language)
