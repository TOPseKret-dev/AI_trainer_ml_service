from typing import Annotated

from pydantic import Field

from app.schemas.common import NonEmptyString, StrictModel


# PROVISIONAL CONTRACT: TODO confirm transcription response with backend ↔ ML.
class TranscriptionResponse(StrictModel):
    text: str
    language: NonEmptyString | None
    duration_seconds: Annotated[float, Field(ge=0)] | None
    confidence: Annotated[float, Field(ge=0, le=1)] | None
