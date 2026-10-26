from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.common import (
    Criterion,
    DialogueMessage,
    NonEmptyString,
    NonNegativeInt,
    StrictModel,
)


class InterviewerRequest(StrictModel):
    question: NonEmptyString
    criteria: list[Criterion] = Field(min_length=1)
    dialogue: list[DialogueMessage] = Field(min_length=1)
    follow_up_count: NonNegativeInt
    max_follow_ups: NonNegativeInt


class InterviewerResponse(StrictModel):
    action: Literal["follow_up", "complete"]
    message: NonEmptyString | None

    @model_validator(mode="after")
    def validate_action_message(self) -> Self:
        if self.action == "follow_up" and self.message is None:
            raise ValueError("follow_up requires a non-empty message")
        if self.action == "complete" and self.message is not None:
            raise ValueError("complete requires message=null")
        return self
