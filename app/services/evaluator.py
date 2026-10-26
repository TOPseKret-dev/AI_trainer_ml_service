import logging

from app.schemas.evaluator import CriterionEvaluation, EvaluatorRequest, EvaluatorResponse

logger = logging.getLogger(__name__)


def validate_evaluation(request: EvaluatorRequest, response: EvaluatorResponse) -> None:
    """Validate inference output against the request-specific rubric before returning it."""
    rubric = {criterion.criterion_id: criterion for criterion in request.criteria}
    output_ids = [criterion.criterion_id for criterion in response.criteria]
    if len(output_ids) != len(rubric) or set(output_ids) != set(rubric):
        raise ValueError("Evaluation must return exactly the input criterion IDs")
    for result in response.criteria:
        scores = {anchor.score for anchor in rubric[result.criterion_id].anchors}
        if result.score not in scores:
            raise ValueError("Evaluation score must be one of the criterion's anchor scores")
        maximum = max(scores)
        if result.status == "partial" and not 0 < result.score < maximum:
            raise ValueError("partial requires a positive anchor score below the maximum")
        if result.status == "correct" and result.score != maximum:
            raise ValueError("correct requires the maximum anchor score")


async def evaluate_answer(request: EvaluatorRequest) -> EvaluatorResponse:
    # TODO replace this deterministic stub with real evaluation, then keep rubric validation.
    result = EvaluatorResponse(
        criteria=[
            CriterionEvaluation(
                criterion_id=criterion.criterion_id,
                score=0,
                status="absent",
                comment="Stub evaluator: real evaluation is not implemented yet.",
                evidence=[],
            )
            for criterion in request.criteria
        ],
        summary="Stub evaluation: real LLM evaluation is not implemented yet.",
        recommendations=[],
    )
    validate_evaluation(request, result)
    logger.info("function=evaluate_answer outcome=success criteria_count=%d", len(result.criteria))
    return result
