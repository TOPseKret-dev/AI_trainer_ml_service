import logging
from typing import BinaryIO

from app.schemas.transcription import TranscriptionResponse

logger = logging.getLogger(__name__)


async def transcribe_audio(audio: BinaryIO, language: str = "ru") -> TranscriptionResponse:
    """Accept a validated binary audio stream positioned at its start."""
    # TODO replace this deterministic stub with real ASR inference; do not invent metadata.
    result = TranscriptionResponse(
        text="stub transcription",
        language=language,
        duration_seconds=None,
        confidence=None,
    )
    logger.info("function=transcribe_audio outcome=success implementation=stub")
    return result
