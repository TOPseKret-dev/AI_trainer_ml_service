"""MI-007 error catalogue. Provider/user text never becomes a public message."""

from dataclasses import dataclass
from enum import StrEnum


class MLErrorCode(StrEnum):
    INVALID_INPUT = "ML_INVALID_INPUT"
    NOTHING_TO_EVALUATE = "ML_NOTHING_TO_EVALUATE"
    PROVIDER_NOT_CONFIGURED = "ML_PROVIDER_NOT_CONFIGURED"
    PROVIDER_AUTH_ERROR = "ML_PROVIDER_AUTH_ERROR"
    PROVIDER_QUOTA_EXCEEDED = "ML_PROVIDER_QUOTA_EXCEEDED"
    PROVIDER_CONFIG_ERROR = "ML_PROVIDER_CONFIG_ERROR"
    RATE_LIMITED = "ML_RATE_LIMITED"
    UNAVAILABLE = "ML_UNAVAILABLE"
    QUEUE_FULL = "ML_QUEUE_FULL"
    TIMEOUT = "ML_TIMEOUT"
    QUEUE_TIMEOUT = "ML_QUEUE_TIMEOUT"
    EMPTY_RESPONSE = "ML_EMPTY_RESPONSE"
    OUTPUT_TRUNCATED = "ML_OUTPUT_TRUNCATED"
    SCHEMA_ERROR = "ML_SCHEMA_ERROR"
    INVALID_RESPONSE = "ML_INVALID_RESPONSE"
    POLICY_REFUSAL = "ML_POLICY_REFUSAL"
    INTERNAL_ERROR = "ML_INTERNAL_ERROR"


@dataclass(frozen=True)
class ErrorDefinition:
    status: int
    message: str
    retryable: bool = False
    repairable: bool = False


ERROR_CATALOG = {
    MLErrorCode.INVALID_INPUT: ErrorDefinition(422, "Invalid ML request"),
    MLErrorCode.NOTHING_TO_EVALUATE: ErrorDefinition(422, "No candidate answer to evaluate"),
    MLErrorCode.PROVIDER_NOT_CONFIGURED: ErrorDefinition(
        503, "OPENROUTER_API_KEY is not configured"
    ),
    MLErrorCode.PROVIDER_AUTH_ERROR: ErrorDefinition(503, "ML provider access denied"),
    MLErrorCode.PROVIDER_QUOTA_EXCEEDED: ErrorDefinition(503, "ML provider quota exceeded"),
    MLErrorCode.PROVIDER_CONFIG_ERROR: ErrorDefinition(503, "Invalid ML provider configuration"),
    MLErrorCode.RATE_LIMITED: ErrorDefinition(503, "ML provider rate limited the request", True),
    MLErrorCode.UNAVAILABLE: ErrorDefinition(503, "ML provider is unavailable", True),
    MLErrorCode.QUEUE_FULL: ErrorDefinition(503, "ML queue is full", True),
    MLErrorCode.TIMEOUT: ErrorDefinition(504, "ML provider request timed out", True),
    MLErrorCode.QUEUE_TIMEOUT: ErrorDefinition(504, "ML queue deadline exceeded", True),
    MLErrorCode.EMPTY_RESPONSE: ErrorDefinition(
        502, "ML provider returned empty content", False, True
    ),
    MLErrorCode.OUTPUT_TRUNCATED: ErrorDefinition(502, "ML output was truncated", False, True),
    MLErrorCode.SCHEMA_ERROR: ErrorDefinition(
        502, "ML output violated the result contract", False, True
    ),
    MLErrorCode.INVALID_RESPONSE: ErrorDefinition(502, "ML provider returned an invalid response"),
    MLErrorCode.POLICY_REFUSAL: ErrorDefinition(422, "ML provider refused the request"),
    MLErrorCode.INTERNAL_ERROR: ErrorDefinition(500, "Internal ML service error"),
}


class MLServiceError(Exception):
    def __init__(self, code: MLErrorCode, *, retry_after_seconds: int | None = None) -> None:
        self.code = code
        definition = ERROR_CATALOG[code]
        self.status_code = definition.status
        self.detail = definition.message
        self.retryable = definition.retryable
        self.repairable = definition.repairable
        self.retry_after_seconds = retry_after_seconds if code == MLErrorCode.RATE_LIMITED else None
        super().__init__(self.detail)


class ProviderUnavailable(MLServiceError):
    def __init__(self) -> None:
        super().__init__(MLErrorCode.UNAVAILABLE)


class ProviderNotConfigured(MLServiceError):
    def __init__(self) -> None:
        super().__init__(MLErrorCode.PROVIDER_NOT_CONFIGURED)


class ProviderTimeout(MLServiceError):
    def __init__(self) -> None:
        super().__init__(MLErrorCode.TIMEOUT)


class InvalidProviderResponse(MLServiceError):
    def __init__(self) -> None:
        super().__init__(MLErrorCode.INVALID_RESPONSE)
