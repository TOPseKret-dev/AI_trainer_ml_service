"""PROVISIONAL CONTRACT: TODO agree draft generation payloads with backend ↔ ML."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from app.schemas.common import (
    GRADE_ORDER,
    Criterion,
    Grade,
    NonEmptyString,
    PositiveInt,
    ScoreAnchor,
    StrictModel,
    validate_anchors,
)


class QuestionBankGenerationRequest(StrictModel):
    specialization: NonEmptyString
    topic: NonEmptyString
    min_grade: Grade
    difficulty: NonEmptyString
    count: Annotated[int, Field(ge=1, le=50, strict=True)] = 10


class GradeReferenceAnswer(StrictModel):
    grade: Grade
    reference_answer: NonEmptyString


class CriterionGradeConfig(StrictModel):
    grade: Grade
    weight: Annotated[float, Field(gt=0)]
    max_score: PositiveInt
    expected_level: NonEmptyString
    anchors: list[ScoreAnchor] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_rubric(self) -> Self:
        validate_anchors(self.anchors)
        if max(anchor.score for anchor in self.anchors) != self.max_score:
            raise ValueError("max_score must equal the maximum anchor score")
        return self


class DraftCriterion(Criterion):
    grade_configs: list[CriterionGradeConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_grades(self) -> Self:
        grades = [config.grade for config in self.grade_configs]
        if len(grades) != len(set(grades)):
            raise ValueError("Grade configs must be unique within a criterion")
        return self


class DraftQuestion(StrictModel):
    question_text: NonEmptyString
    reference_answers: list[GradeReferenceAnswer] = Field(min_length=1)
    criteria: list[DraftCriterion] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("Criterion IDs must be unique within a draft")
        grades = [answer.grade for answer in self.reference_answers]
        first_grade = min(grades, key=GRADE_ORDER.index)
        if grades != list(GRADE_ORDER[GRADE_ORDER.index(first_grade) :]):
            raise ValueError("Reference answers must cover consecutive grades through senior")
        for criterion in self.criteria:
            if [config.grade for config in criterion.grade_configs] != grades:
                raise ValueError("Each criterion must cover the reference answer grades in order")
        return self


class QuestionBankGenerationResponse(StrictModel):
    questions: list[DraftQuestion] = Field(min_length=1, max_length=50)
