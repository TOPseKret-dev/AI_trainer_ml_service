"""Compile disjoint score bands; the model authors the criterion's full-score requirements.

This policy is applied before independent review, never to already reviewed content.
The original provider response remains in the journal for audit.
"""

from .contracts import Candidate

ZERO = (
    "Нет ни одного корректно раскрытого существенного элемента требований уровня 2: "
    "ответ отсутствует, не относится к критерию или полностью неверен."
)
PARTIAL = (
    "Есть хотя бы один корректный существенный элемент, относящийся к критерию, "
    "однако ответ не достигает требований полного балла: часть обязательного обоснования "
    "отсутствует или содержит содержательную ошибку."
)


def compile_rubric(candidate: Candidate) -> Candidate:
    value = candidate.model_dump()
    for criterion in value["criteria"]:
        for config in criterion["grade_configs"]:
            config["anchors"][0]["description"] = ZERO
            config["anchors"][1]["description"] = PARTIAL
    return Candidate.model_validate(value)
