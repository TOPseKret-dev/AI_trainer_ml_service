"""HTTP contract for autonomous draft generation, independent of MI-007 runtime calls."""

from enum import StrEnum

from pydantic import Field

from app.question_generation.contracts import Contract, GenerationRequest, GenerationResult


class QuestionGenerationRequest(GenerationRequest):
    # HTTP retries must always address a durable, backend-owned operation.
    request_id: str = Field(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        json_schema_extra={"format": "uuid"},
    )


class QuestionGenerationResponse(GenerationResult):
    """Terminal generation result; failed/partial are explicit business outcomes."""


class QuestionGenerationErrorCode(StrEnum):
    INVALID_INPUT = "QGEN_INVALID_INPUT"
    PAYLOAD_TOO_LARGE = "QGEN_PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "QGEN_UNSUPPORTED_MEDIA_TYPE"
    REQUEST_TIMEOUT = "QGEN_REQUEST_TIMEOUT"
    IDEMPOTENCY_CONFLICT = "QGEN_IDEMPOTENCY_CONFLICT"
    JOB_BUSY = "QGEN_JOB_BUSY"
    CAPACITY_EXCEEDED = "QGEN_CAPACITY_EXCEEDED"
    PROVIDER_NOT_CONFIGURED = "QGEN_PROVIDER_NOT_CONFIGURED"
    STORAGE_UNAVAILABLE = "QGEN_STORAGE_UNAVAILABLE"
    INTERNAL_ERROR = "QGEN_INTERNAL_ERROR"


class QuestionGenerationErrorDetails(Contract):
    correlation_id: str
    request_id: str | None = None
    retryable: bool
    retry_after_seconds: int | None = Field(default=None, ge=0)


class QuestionGenerationErrorBody(Contract):
    code: QuestionGenerationErrorCode
    message: str
    details: QuestionGenerationErrorDetails


class QuestionGenerationErrorResponse(Contract):
    error: QuestionGenerationErrorBody
