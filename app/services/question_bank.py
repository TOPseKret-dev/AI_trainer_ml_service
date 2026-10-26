import logging

from app.schemas.common import GRADE_ORDER, ScoreAnchor
from app.schemas.question_bank import (
    CriterionGradeConfig,
    DraftCriterion,
    DraftQuestion,
    GradeReferenceAnswer,
    QuestionBankGenerationRequest,
    QuestionBankGenerationResponse,
)

logger = logging.getLogger(__name__)


async def generate_question_bank(
    request: QuestionBankGenerationRequest,
) -> QuestionBankGenerationResponse:
    # PROVISIONAL CONTRACT: TODO replace after agreement between backend ↔ ML.
    # TODO replace deterministic placeholders with inference; return drafts without persistence.
    grades = GRADE_ORDER[GRADE_ORDER.index(request.min_grade) :]
    questions = [
        DraftQuestion(
            question_text=f"[STUB {index}] Технический вопрос по теме {request.topic}",
            reference_answers=[
                GradeReferenceAnswer(
                    grade=grade,
                    reference_answer=f"[STUB] Эталонный ответ для грейда {grade.value}.",
                )
                for grade in grades
            ],
            criteria=[
                DraftCriterion(
                    criterion_id=f"stub_criterion_{index}",
                    description=f"[STUB] Критерий по теме {request.topic}.",
                    grade_configs=[
                        CriterionGradeConfig(
                            grade=grade,
                            weight=1.0,
                            max_score=3,
                            expected_level=f"[STUB] Ожидаемый уровень: {grade.value}.",
                            anchors=[
                                ScoreAnchor(
                                    score=score,
                                    description=f"[STUB] Описание балла {score}.",
                                )
                                for score in range(4)
                            ],
                        )
                        for grade in grades
                    ],
                )
            ],
        )
        for index in range(1, request.count + 1)
    ]
    result = QuestionBankGenerationResponse(questions=questions)
    logger.info(
        "function=generate_question_bank outcome=success draft_count=%d", len(result.questions)
    )
    return result
