from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyString = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Grade(StrEnum):
    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"


GRADE_ORDER = (Grade.JUNIOR, Grade.MIDDLE, Grade.SENIOR)


class HealthResponse(StrictModel):
    status: Literal["ok"]
    service: Literal["ml-service"]


class DialogueMessage(StrictModel):
    role: Literal["candidate", "interviewer"]
    content: NonEmptyString


class Criterion(StrictModel):
    criterion_id: NonEmptyString
    description: NonEmptyString


class ScoreAnchor(StrictModel):
    score: NonNegativeInt
    description: NonEmptyString


def validate_anchors(anchors: list[ScoreAnchor]) -> None:
    scores = [anchor.score for anchor in anchors]
    if len(scores) != len(set(scores)):
        raise ValueError("Anchor scores must be unique within a criterion")
    if 0 not in scores:
        raise ValueError("Anchors must include score 0")
    if not any(score > 0 for score in scores):
        raise ValueError("Anchors must include a positive score")
