"""Strict public contracts for the autonomous question generation callable.

The MVP uses a permissible stricter subset of the rubric contract: every
criterion covers every candidate grade (dense criterion configurations).
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Grade = Literal["Junior", "Middle", "Senior"]
GRADES: tuple[Grade, ...] = ("Junior", "Middle", "Senior")
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Area = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Skill = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Comment = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class Contract(BaseModel):
    """Common validation policy, inherited by every nested object."""

    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, revalidate_instances="always"
    )


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class ExistingQuestion(Contract):
    id: Identifier
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    area: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    skills: list[Skill] = Field(default_factory=list, max_length=10)
    min_grade: Grade = "Junior"


class Material(Contract):
    id: Identifier
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]


class GenerationRequest(Contract):
    request_id: str | None = None
    specialization: Literal["analytics"] = "analytics"
    area: Area
    grade: Grade
    count: int = Field(default=3, ge=1, le=10)
    comment: Comment = ""
    existing_questions: list[ExistingQuestion] = Field(default_factory=list, max_length=500)
    bank_context_complete: bool = False
    materials: list[Material] = Field(default_factory=list, max_length=20)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str | None) -> str | None:
        if value is not None:
            return str(UUID(value))
        return value

    @property
    def grades(self) -> list[Grade]:
        """The requested minimum grade and all higher grades."""
        return list(GRADES[GRADES.index(self.grade) :])

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _unique([question.id for question in self.existing_questions], "Existing question ids")
        _unique([material.id for material in self.materials], "Material ids")
        if any(m.id.startswith("matrix:") for m in self.materials):
            raise ValueError("matrix: material id prefix is reserved for bundled sources")
        if len(self.model_dump_json().encode("utf-8")) > 120000:
            raise ValueError("Serialized request must not exceed 120000 UTF-8 bytes")
        return self


class PlanTarget(Contract):
    intent: Annotated[str, StringConstraints(strip_whitespace=True, min_length=15, max_length=600)]
    boundaries: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=600)
    ]


class GenerationPlan(Contract):
    targets: list[PlanTarget] = Field(min_length=1, max_length=10)


class Anchor(Contract):
    score: int = Field(ge=0, le=2)
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=5, max_length=1500)
    ]


class CriterionGradeConfig(Contract):
    grade: Grade
    weight: float = Field(gt=0, le=10)
    anchors: list[Anchor] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_anchors(self) -> Self:
        if [anchor.score for anchor in self.anchors] != [0, 1, 2]:
            raise ValueError("Anchor scores must be ordered exactly as 0, 1, 2")
        _unique([anchor.description for anchor in self.anchors], "Anchor descriptions")
        return self


class Criterion(Contract):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)
    ]
    grade_configs: list[CriterionGradeConfig] = Field(min_length=1, max_length=3)


class Reference(Contract):
    grade: Grade
    reference_answer: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=30, max_length=10000)
    ]


class Candidate(Contract):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=2000)]
    skills: list[Skill] = Field(min_length=1, max_length=4)
    criteria: list[Criterion] = Field(min_length=1, max_length=4)
    grade_configs: list[Reference] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        _unique([criterion.id for criterion in self.criteria], "Criterion ids")
        _unique([criterion.description for criterion in self.criteria], "Criterion descriptions")
        grades = [reference.grade for reference in self.grade_configs]
        if grades != list(GRADES[GRADES.index(grades[0]) :]):
            raise ValueError("Reference grades must be consecutive and ordered through Senior")
        for criterion in self.criteria:
            if [config.grade for config in criterion.grade_configs] != grades:
                raise ValueError("Every criterion must have the same ordered grades as references")
        return self


class ReviewChecks(Contract):
    factual_correctness: bool
    scope_alignment: bool
    grade_fit: bool
    rubric_alignment: bool
    reference_completeness: bool
    distinctness: bool


class Defect(Contract):
    severity: Literal["minor", "major", "critical"]
    path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    explanation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)
    ]
    required_change: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)
    ]


class Review(Contract):
    checks: ReviewChecks
    defects: list[Defect] = Field(max_length=20)
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]

    @model_validator(mode="after")
    def validate_coherence(self) -> Self:
        all_passed = all(self.checks.model_dump().values())
        has_blocking_defect = any(d.severity in {"major", "critical"} for d in self.defects)
        if not all_passed and not has_blocking_defect:
            raise ValueError("Failed checks require at least one major or critical defect")
        if all_passed and has_blocking_defect:
            raise ValueError("Major or critical defects cannot accompany all passing checks")
        return self


class Metadata(Contract):
    source: Literal["model_generated"] = "model_generated"
    comment: Comment
    model: Identifier
    reviewer_model: Identifier
    prompt_version: Identifier
    plan_target: str | None = Field(default=None, max_length=600)
    material_ids: list[Identifier] = Field(max_length=40)
    quality_status: Literal["passed"] = "passed"
    review: Review
    distinctness_review: Review | None = None

    @model_validator(mode="after")
    def validate_metadata(self) -> Self:
        _unique(self.material_ids, "Material ids")
        if self.distinctness_review and not all(
            self.distinctness_review.checks.model_dump().values()
        ):
            raise ValueError("Passed quality status requires passing distinctness review")
        if not all(self.review.checks.model_dump().values()):
            raise ValueError("Passed quality status requires all review checks to pass")
        return self


class DraftQuestion(Candidate):
    id: Identifier
    specialization: Literal["analytics"] = "analytics"
    area: Area
    min_grade: Grade
    grades: list[Grade] = Field(min_length=1, max_length=3)
    status: Literal["draft"] = "draft"
    generation: Metadata

    @model_validator(mode="after")
    def validate_draft_grades(self) -> Self:
        expected = list(GRADES[GRADES.index(self.min_grade) :])
        if self.grades != expected or [ref.grade for ref in self.grade_configs] != expected:
            raise ValueError("Draft grades and references must run from min_grade through Senior")
        return self


class RejectedCandidate(Contract):
    candidate: Candidate
    issues: list[Defect] = Field(max_length=20)
    quality_status: Literal["needs_revision", "unverified"]
    slot: int = Field(ge=0)


class Warning(Contract):
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
    slot: int | None = Field(default=None, ge=0)


class Metrics(Contract):
    calls: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    reserved_uncertain_usd: float = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)


class GenerationResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    job_id: Identifier
    status: Literal["completed", "partial", "failed", "cancelled"]
    questions: list[DraftQuestion] = Field(max_length=10)
    rejected_candidates: list[RejectedCandidate]
    warnings: list[Warning]
    metrics: Metrics


__all__ = [
    "Grade",
    "Contract",
    "GenerationRequest",
    "ExistingQuestion",
    "Material",
    "Anchor",
    "CriterionGradeConfig",
    "Criterion",
    "Reference",
    "Candidate",
    "ReviewChecks",
    "Defect",
    "Review",
    "Metadata",
    "DraftQuestion",
    "RejectedCandidate",
    "Warning",
    "Metrics",
    "GenerationResult",
]
