from typing import Annotated, Generic, Literal, TypeVar

from pydantic import Field, StrictBool, model_serializer

from app.errors import MLErrorCode
from app.schemas.common import NonEmptyString, NonNegativeInt, StrictModel

Operation = Literal["interviewer", "evaluator"]
OperationT = TypeVar("OperationT", bound=Operation)
ProviderAttempts = Annotated[int, Field(ge=0, le=2, strict=True)]


def omit_internal_defaults(schema: dict) -> None:
    for prop in schema.get("properties", {}).values():
        if prop.get("default", ...) is None:
            prop.pop("default")


class MLResponseMeta(StrictModel):
    prompt_tokens: NonNegativeInt | None
    completion_tokens: NonNegativeInt | None
    reasoning_tokens: NonNegativeInt | None = None
    duration_ms: NonNegativeInt
    provider_attempts: ProviderAttempts


class ErrorObject(StrictModel):
    model_config = {"json_schema_extra": omit_internal_defaults}

    @model_serializer(mode="wrap")
    def omit_missing(self, handler):
        # Optional objects have an internal None default, never a wire null.
        # Metadata uses its own serializer and retains mandatory unknown token nulls.
        data = handler(self)
        if data is None:
            return None
        return {key: value for key, value in data.items() if value is not None}


class MLErrorDetails(ErrorObject, Generic[OperationT]):
    operation: OperationT = Field(default=None)
    retryable: StrictBool = Field(default=None)
    retry_after_seconds: NonNegativeInt | None = None
    correlation_id: str | None = None
    provider_attempts: ProviderAttempts | None = None


class MLErrorBody(ErrorObject, Generic[OperationT]):
    code: MLErrorCode
    message: NonEmptyString
    details: MLErrorDetails[OperationT] = Field(default=None)


class MLErrorResponse(ErrorObject, Generic[OperationT]):
    error: MLErrorBody[OperationT]
    meta: MLResponseMeta = Field(default=None)


InterviewerErrorResponse = MLErrorResponse[Literal["interviewer"]]
EvaluatorErrorResponse = MLErrorResponse[Literal["evaluator"]]
