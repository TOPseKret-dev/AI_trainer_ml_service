"""Explicit draft-only projections into the current knowledge model and ML runtime."""

from .contracts import DraftQuestion

GRADE_NUMBERS = {"Junior": 1, "Middle": 2, "Senior": 3}


def knowledge_rows(question: DraftQuestion | dict):
    """Core assigns database IDs and maps area to its topic dictionary, in one transaction."""
    q = DraftQuestion.model_validate(question)
    code = "gen_" + q.id.replace("-", "_")
    criteria, configs, anchors, references = [], [], [], []
    for order, c in enumerate(q.criteria):
        criteria.append(
            {"question_code": code, "code": c.id, "description": c.description, "sort_order": order}
        )
        for g in c.grade_configs:
            key = {"question_code": code, "criterion_code": c.id, "grade": GRADE_NUMBERS[g.grade]}
            configs.append({**key, "weight": g.weight})
            anchors.extend({**key, **a.model_dump()} for a in g.anchors)
    for g in q.grade_configs:
        references.append(
            {
                "question_code": code,
                "grade": GRADE_NUMBERS[g.grade],
                "reference_answer": g.reference_answer,
            }
        )
    return {
        "questions": [
            {
                "code": code,
                "specialization": "analyst",
                "topic": q.area,
                "question_text": q.text,
                "min_grade": GRADE_NUMBERS[q.min_grade],
                "version": 1,
                "is_active": False,
            }
        ],
        "criteria": criteria,
        "criterion_grade_configs": configs,
        "criterion_grade_anchors": anchors,
        "question_grade_configs": references,
    }


def runtime_payloads(question: DraftQuestion | dict, grade: str):
    """Match adjacent ML interviewer/evaluator fields, without leaking rubric answers."""
    q = DraftQuestion.model_validate(question)
    if grade not in q.grades:
        raise ValueError("grade not applicable; use Junior, Middle or Senior")
    interviewer = {
        "question": q.text,
        "criteria": [{"criterion_id": c.id, "description": c.description} for c in q.criteria],
    }
    evaluator = {
        "question": q.text,
        "target_level": grade.lower(),
        "reference_answer": next(g.reference_answer for g in q.grade_configs if g.grade == grade),
        "criteria": [
            {
                "criterion_id": c.id,
                "description": c.description,
                "anchors": [
                    a.model_dump() for g in c.grade_configs if g.grade == grade for a in g.anchors
                ],
            }
            for c in q.criteria
        ],
    }
    return interviewer, evaluator
