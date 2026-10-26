import logging

from app.clients.openrouter import OpenRouterClient
from app.prompts.evaluator import EVALUATOR_PROMPT_VERSION, EVALUATOR_SYSTEM_PROMPT
from app.schemas.evaluator import EvaluatorRequest, EvaluatorResponse
from app.services.structured import DomainViolation, build_messages, generate_structured

logger = logging.getLogger(__name__)


def validate_evaluation(request: EvaluatorRequest, response: EvaluatorResponse) -> None:
    """Validate inference output against the request-specific rubric and candidate text."""
    rubric = {criterion.criterion_id: criterion for criterion in request.criteria}
    output_ids = [criterion.criterion_id for criterion in response.criteria]
    if (
        len(output_ids) != len(set(output_ids))
        or len(output_ids) != len(rubric)
        or set(output_ids) != set(rubric)
    ):
        raise DomainViolation("Evaluation must return exactly the input criterion IDs once each.")
    candidate_texts = [
        message.content for message in request.dialogue if message.role == "candidate"
    ]
    for index, result in enumerate(response.criteria):
        prefix = f"Output criterion at index {index}: "
        scores = {anchor.score for anchor in rubric[result.criterion_id].anchors}
        if result.score not in scores:
            raise DomainViolation(prefix + f"score must be in anchors {sorted(scores)}.")
        maximum = max(scores)
        if result.status in {"absent", "incorrect"} and result.score != 0:
            raise DomainViolation(prefix + "absent/incorrect require score 0.")
        if result.status == "absent" and result.evidence:
            raise DomainViolation(prefix + "absent requires empty evidence.")
        if result.status == "partial" and not 0 < result.score < maximum:
            raise DomainViolation(prefix + "partial requires an anchor between 0 and max_score.")
        if result.status == "correct" and result.score != maximum:
            raise DomainViolation(prefix + "correct requires the maximum anchor score.")
        if result.status != "absent" and not result.evidence:
            raise DomainViolation(prefix + "non-absent status requires candidate evidence.")
        for evidence in result.evidence:
            if not evidence.strip() or not any(evidence in text for text in candidate_texts):
                raise DomainViolation(
                    prefix + "evidence must be a verbatim substring of a candidate message."
                )


async def evaluate_answer(request: EvaluatorRequest, client: OpenRouterClient) -> EvaluatorResponse:
    result = await generate_structured(
        client=client,
        messages=build_messages(
            EVALUATOR_SYSTEM_PROMPT,
            request.model_dump_json(exclude={"dialogue"}),
            request.dialogue,
        ),
        response_model=EvaluatorResponse,
        service="evaluator",
        prompt_version=EVALUATOR_PROMPT_VERSION,
        temperature=0.0,
        validate=lambda response: validate_evaluation(request, response),
    )
    logger.info("function=evaluate_answer outcome=success criteria_count=%d", len(result.criteria))
    return result
