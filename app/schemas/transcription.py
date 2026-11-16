from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TranscriptionResponse(BaseModel):
    attempt_id: UUID
    recognized_text: str = Field(min_length=1)
    audio_duration_ms: int = Field(ge=0)
    language: str
    provider: str
    model: str
    ml_received_at: datetime
    queue_ms: int = Field(ge=0)
    processing_ms: int = Field(ge=0)
    glossary_version: str | None
