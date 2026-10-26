from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.common import (
    Criterion,
    DialogueMessage,
    Grade,
    NonEmptyString,
    NonNegativeInt,
    ScoreAnchor,
    StrictModel,
    validate_anchors,
)


class EvaluatorCriterion(Criterion):
    anchors: list[ScoreAnchor] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_rubric(self) -> Self:
        validate_anchors(self.anchors)
        return self


class EvaluatorRequest(StrictModel):
    question: NonEmptyString
    target_level: Grade
    reference_answer: NonEmptyString
    criteria: list[EvaluatorCriterion] = Field(min_length=1)
    dialogue: list[DialogueMessage] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_criterion_ids(self) -> Self:
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("Criterion IDs must be unique")
        return self


class CriterionEvaluation(StrictModel):
    criterion_id: NonEmptyString
    score: NonNegativeInt
    status: Literal["absent", "incorrect", "partial", "correct"]
    comment: NonEmptyString
    evidence: list[NonEmptyString] = Field(json_schema_extra={"uniqueItems": True})

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        if self.status in {"absent", "incorrect"} and self.score != 0:
            raise ValueError("absent and incorrect require score 0")
        if self.status == "absent" and self.evidence:
            raise ValueError("absent requires empty evidence")
        if self.status in {"partial", "correct"} and self.score <= 0:
            raise ValueError("partial and correct require a positive score")
        if len(self.evidence) != len(set(self.evidence)):
            raise ValueError("Evidence entries must be unique")
        return self


class EvaluatorResponse(StrictModel):
    criteria: list[CriterionEvaluation] = Field(min_length=1)
    summary: NonEmptyString
    recommendations: list[NonEmptyString]

    @model_validator(mode="after")
    def validate_criterion_ids(self) -> Self:
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("Output criterion IDs must be unique")
        return self
