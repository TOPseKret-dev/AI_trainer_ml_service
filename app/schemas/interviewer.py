from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.common import (
    Criterion,
    DialogueMessage,
    NonEmptyString,
    NonNegativeInt,
    StrictModel,
)
from app.schemas.deadline import DeadlineUTC
from app.schemas.meta import MLResponseMeta


class InterviewerRequest(StrictModel):
    question: NonEmptyString
    criteria: list[Criterion] = Field(min_length=1, json_schema_extra={"uniqueItems": True})
    dialogue: list[DialogueMessage] = Field(min_length=1)
    follow_up_count: NonNegativeInt
    max_follow_ups: NonNegativeInt
    deadline_at: DeadlineUTC = None

    @model_validator(mode="after")
    def validate_criterion_ids(self) -> Self:
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("Criterion IDs must be unique")
        return self


class InterviewerLLMResult(StrictModel):
    action: Literal["follow_up", "complete"]
    message: NonEmptyString | None

    model_config = {
        "json_schema_extra": {
            "oneOf": [
                {"properties": {"action": {"const": "follow_up"}, "message": {"type": "string"}}},
                {"properties": {"action": {"const": "complete"}, "message": {"type": "null"}}},
            ]
        }
    }

    @model_validator(mode="after")
    def validate_action_message(self) -> Self:
        if self.action == "follow_up" and self.message is None:
            raise ValueError("follow_up requires a non-empty message")
        if self.action == "complete" and self.message is not None:
            raise ValueError("complete requires message=null")
        return self


class InterviewerResponse(InterviewerLLMResult):
    meta: MLResponseMeta
