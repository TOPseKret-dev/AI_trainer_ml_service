# Upstream fixture strings and immediately awaited loop closures are preserved.
# ruff: noqa: E501, B023
"""Offline behavioral tests for the autonomous generation API.

The scripted provider is deliberately strict: an unexpected call fails the test.
Crash tests reuse a real temporary journal with a fresh generator instance.
"""

import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from app.question_generation import (
    GenerationRequest,
    GenerationSettings,
    IdempotencyConflict,
    JobBusy,
    ModelPolicy,
    QuestionGenerator,
)
from app.question_generation.adapters import knowledge_rows, runtime_payloads
from app.question_generation.contracts import Candidate, GenerationPlan, Review
from app.question_generation.journal import Journal
from app.question_generation.prompts import COMMON, DISTINCT, VERSION
from app.question_generation.provider import Completion, ProviderFailure
from app.question_generation.rubric import PARTIAL, ZERO, compile_rubric

GRADES = ["Junior", "Middle", "Senior"]


def candidate(grade="Middle", *, text=None):
    grades = GRADES[GRADES.index(grade) :]
    return compile_rubric(
        Candidate.model_validate(
            {
                "text": text
                or "Как в PostgreSQL различаются COUNT(*) и COUNT(email), если email допускает NULL?",
                "skills": ["SQL: агрегатные функции"],
                "criteria": [
                    {
                        "id": key,
                        "description": description,
                        "grade_configs": [
                            {
                                "grade": g,
                                "weight": 1.0,
                                "anchors": [
                                    {
                                        "score": 0,
                                        "description": "Не объясняет поведение функции или объясняет неверно.",
                                    },
                                    {"score": 1, "description": partial},
                                    {"score": 2, "description": full},
                                ],
                            }
                            for g in grades
                        ],
                    }
                    for key, description, partial, full in [
                        (
                            "rows",
                            "Понимание подсчёта строк таблицы",
                            "Верно считает строки только для примера без NULL.",
                            "Объясняет, что COUNT(*) считает все строки независимо от NULL.",
                        ),
                        (
                            "values",
                            "Понимание подсчёта значений столбца",
                            "Указывает исключение NULL, но не объясняет случай всех NULL.",
                            "Объясняет исключение NULL и нулевой результат для столбца из одних NULL.",
                        ),
                    ]
                ],
                "grade_configs": [
                    {
                        "grade": g,
                        "reference_answer": "COUNT(*) считает все строки. COUNT(email) считает только строки с email IS NOT NULL. "
                        "Если все email равны NULL, COUNT(email) равен нулю; для пустой таблицы оба дают ноль.",
                    }
                    for g in grades
                ],
            }
        )
    )


def review(failed=None):
    checks = dict.fromkeys(
        [
            "factual_correctness",
            "scope_alignment",
            "grade_fit",
            "rubric_alignment",
            "reference_completeness",
            "distinctness",
        ],
        True,
    )
    defects = []
    if failed:
        checks[failed] = False
        defects = [
            {
                "severity": "major",
                "path": "grade_configs.0.reference_answer",
                "explanation": "Не разобран случай пустой таблицы.",
                "required_change": "Добавить результат для пустой таблицы.",
            }
        ]
    return Review(checks=checks, defects=defects, summary="Проверка комплекта завершена.")


def completion(value, cost=0.01, finish="stop"):
    content = (
        value.model_dump_json() if isinstance(value, (Candidate, Review, GenerationPlan)) else value
    )
    return Completion(content, cost, "", finish)


class Crash(BaseException):
    """Simulate process loss without translating it into a provider error."""


class BlockedCall:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def __call__(self):
        self.started.set()
        try:
            await self.release.wait()
            return completion(review())
        finally:
            self.finished.set()


class ScriptedProvider:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def complete(self, request, timeout):
        self.calls.append(copy.deepcopy(request))
        if not self.steps:
            raise AssertionError("Unexpected provider call")
        if timeout <= 0:
            raise AssertionError("Provider timeout must be positive")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, BlockedCall):
            step = await step()
        if not isinstance(step, Completion):
            raise AssertionError("Fake must return Completion")
        return Completion(
            step.content,
            step.cost_usd,
            request["model"],
            step.finish_reason,
            f"fake-{len(self.calls)}",
        )


def payload(call):
    return json.loads(call["messages"][-1]["content"])


class GenerationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch(
            "socket.socket.connect", side_effect=AssertionError("Live sockets forbidden")
        )
        network.start()
        self.addCleanup(network.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        defaults = GenerationSettings()
        # Budget tests exercise accounting under stable, explicitly cheap caps;
        # production model IDs still follow defaults, but production prices need
        # not fit synthetic budgets such as the 0.10 USD review gate.
        policies = {
            name: ModelPolicy(
                model=getattr(defaults, name).model,
                input_usd_per_million=0.5,
                output_usd_per_million=2.5,
            )
            for name in ("writer", "fallback_writer", "reviewer")
        }
        self.settings = GenerationSettings(
            **policies,
            max_cost_usd=0.75,
            max_rounds=1,
            attempts_per_stage=1,
            semantic_deduplication=False,
            plan_slots=False,
        )
        # A second safety net: even an accidental real adapter invocation cannot pay.
        self.network_guard = patch(
            "app.question_generation.provider.OpenRouterProvider.complete",
            side_effect=AssertionError("Network calls are forbidden in this suite"),
        )
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def request(self, **changes):
        return {
            "request_id": str(uuid4()),
            "area": "SQL",
            "grade": "Middle",
            "count": 1,
            "bank_context_complete": True,
            **changes,
        }

    def generator(self, provider, **settings):
        config = GenerationSettings(**{**self.settings.model_dump(), **settings})
        return QuestionGenerator(provider, state_dir=self.temp.name, settings=config)

    def codes(self, result):
        return {w.code for w in result.warnings}

    async def test_public_request_validation_precedes_provider_and_journal(self):
        valid_existing = {"id": "old", "text": "Существующий вопрос", "area": "SQL"}
        cases = [
            {"count": 0},
            {"count": 11},
            {"count": True},
            {"count": "1"},
            {"grade": "Lead"},
            {"area": "  "},
            {"specialization": "engineering"},
            {"request_id": "../../escape"},
            {"max_cost_usd": 10},
            {"bank_context_complete": "true"},
            {"comment": "я" * 501},
            {"existing_questions": [valid_existing, valid_existing]},
            {"existing_questions": [{**valid_existing, "secret": True}]},
            {"materials": [{"id": "m", "text": "a"}, {"id": "m", "text": "b"}]},
            {"materials": [{"id": str(i), "text": "я" * 12000} for i in range(6)]},
        ]
        provider = ScriptedProvider()
        for changes in cases:
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValidationError):
                    await self.generator(provider).generate(self.request(**changes))
        self.assertEqual(provider.calls, [])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def distinct_bank(self):
        return [
            {
                "id": "bank-count",
                "text": "Чем отличается подсчёт всех строк от подсчёта непустых телефонов в PostgreSQL?",
                "area": "SQL",
                "skills": ["SQL: агрегатные функции"],
                "min_grade": "Middle",
            }
        ]

    def duplicate_review(self):
        value = review("distinctness").model_dump()
        value["defects"] = [
            {
                "severity": "major",
                "path": "text",
                "explanation": "Смысловой дубль bank-count: замена телефона на email не меняет навык.",
                "required_change": "Выбрать другой навык, а не переименовывать сущности.",
            }
        ]
        return Review.model_validate(value)

    def distinct_repair(self):
        value = candidate().model_dump()
        value["text"] = "Как WHERE и HAVING различаются по этапу фильтрации в PostgreSQL?"
        value["skills"] = ["SQL: фильтрация групп"]
        for criterion, key, description, full in zip(
            value["criteria"],
            ("rows", "groups"),
            ("Понимание фильтрации исходных строк", "Понимание фильтрации результатов группировки"),
            (
                "WHERE фильтрует исходные строки до группировки.",
                "HAVING фильтрует группы после группировки и допускает условие на агрегат.",
            ),
            strict=False,
        ):
            criterion.update(id=key, description=description)
            for config in criterion["grade_configs"]:
                config["anchors"][2]["description"] = full
        for ref in value["grade_configs"]:
            ref["reference_answer"] = (
                "WHERE исключает исходные строки до группировки, HAVING исключает группы после неё. "
                "Например, WHERE active = true отбирает строки, а HAVING COUNT(*) > 1 отбирает группы."
            )
        return Candidate.model_validate(value)

    async def test_distinct_rejects_semantic_duplicate_then_reviews_repaired_skill(self):
        duplicate = self.duplicate_review()
        repaired = self.distinct_repair()
        content_pass = review()
        distinct_pass = Review(**{**review().model_dump(), "summary": "Другой навык; дублей нет."})
        provider = ScriptedProvider(
            completion(candidate()),
            completion(content_pass),
            completion(duplicate),
            completion(repaired),
            completion(content_pass),
            completion(distinct_pass),
        )
        result = await self.generator(provider, semantic_deduplication=True, max_rounds=2).generate(
            self.request(existing_questions=self.distinct_bank())
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual([q.text for q in result.questions], [repaired.text])
        self.assertEqual(result.questions[0].generation.review, content_pass)
        self.assertEqual(result.questions[0].generation.distinctness_review, distinct_pass)
        self.assertEqual(result.metrics.calls, 6)
        self.assertAlmostEqual(result.metrics.cost_usd, 0.06)
        self.assertEqual(provider.steps, [])
        self.assertEqual(payload(provider.calls[3])["feedback"], duplicate.model_dump())
        self.assertEqual(provider.calls[3]["model"], self.settings.writer.model)
        self.assertEqual(result.questions[0].generation.model, self.settings.writer.model)
        self.assertEqual(payload(provider.calls[3])["previous_candidate"], candidate().model_dump())
        self.assertEqual(payload(provider.calls[5])["candidate"]["text"], repaired.text)

    async def test_distinct_receives_independent_compact_candidate_and_supplied_bank(self):
        bank = self.distinct_bank()
        original = copy.deepcopy(bank)
        content_pass = Review(**{**review().model_dump(), "summary": "CONTENT_VERDICT_SENTINEL"})
        distinct_pass = Review(**{**review().model_dump(), "summary": "DISTINCT_VERDICT_SENTINEL"})
        provider = ScriptedProvider(
            completion(candidate()), completion(content_pass), completion(distinct_pass)
        )
        result = await self.generator(provider, semantic_deduplication=True).generate(
            self.request(existing_questions=bank, bank_context_complete=False)
        )
        self.assertEqual(result.status, "completed")
        call = provider.calls[2]
        self.assertEqual(
            payload(call),
            {
                "area": "SQL",
                "comment": "",
                "existing_questions": bank,
                "candidate": {
                    "text": candidate().text,
                    "skills": candidate().skills,
                    "criteria": [c.description for c in candidate().criteria],
                },
            },
        )
        self.assertNotIn("CONTENT_VERDICT_SENTINEL", json.dumps(call))
        self.assertNotEqual(call["messages"][0], provider.calls[1]["messages"][0])
        self.assertEqual(call["model"], self.settings.reviewer.model)
        self.assertEqual(call["max_tokens"], self.settings.reviewer_tokens)
        self.assertEqual(
            call["response_format"]["json_schema"]["schema"], Review.model_json_schema()
        )
        self.assertEqual(result.questions[0].generation.distinctness_review, distinct_pass)
        self.assertIn("bank_context_incomplete", self.codes(result))
        self.assertEqual(result.metrics.calls, 3)
        self.assertEqual(bank, original)

    async def test_distinct_checks_previously_accepted_package_questions(self):
        second = self.distinct_repair()
        passed = review()
        provider = ScriptedProvider(
            completion(candidate()),
            completion(passed),
            completion(second),
            completion(passed),
            completion(passed),
        )
        result = await self.generator(provider, semantic_deduplication=True).generate(
            self.request(count=2)
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metrics.calls, 5)
        self.assertIsNone(result.questions[0].generation.distinctness_review)
        self.assertEqual(result.questions[1].generation.distinctness_review, passed)
        existing = payload(provider.calls[4])["existing_questions"]
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing[0]["id"], result.questions[0].id)
        self.assertEqual(existing[0]["text"], result.questions[0].text)
        self.assertEqual(payload(provider.calls[4])["candidate"]["text"], second.text)
        self.assertEqual(provider.steps, [])

    async def test_distinct_rejection_without_repairs_never_accepts_content_pass(self):
        duplicate = self.duplicate_review()
        provider = ScriptedProvider(
            completion(candidate()), completion(review()), completion(duplicate)
        )
        result = await self.generator(provider, semantic_deduplication=True).generate(
            self.request(existing_questions=self.distinct_bank())
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.questions, [])
        self.assertEqual(result.metrics.calls, 3)
        self.assertEqual(result.rejected_candidates[0].candidate, candidate())
        self.assertEqual(result.rejected_candidates[0].issues, duplicate.defects)
        self.assertEqual(result.rejected_candidates[0].quality_status, "needs_revision")

    async def test_distinct_invalid_or_unavailable_never_accepts_candidate(self):
        incoherent = review().model_dump()
        incoherent["checks"]["distinctness"] = False
        for outcome in (
            completion("{broken"),
            completion(json.dumps(incoherent)),
            completion(review(), finish="length"),
            ProviderFailure("http_503"),
            ProviderFailure("http_401", retryable=False, no_charge=True),
        ):
            with self.subTest(outcome=repr(outcome)):
                provider = ScriptedProvider(completion(candidate()), completion(review()), outcome)
                result = await self.generator(provider, semantic_deduplication=True).generate(
                    self.request(existing_questions=self.distinct_bank())
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.questions, [])
                self.assertEqual(result.metrics.calls, 3)
                self.assertEqual(len(result.rejected_candidates), 1)
                self.assertEqual(result.rejected_candidates[0].candidate, candidate())
                self.assertEqual(result.rejected_candidates[0].quality_status, "unverified")
                self.assertEqual(provider.steps, [])

    async def test_distinct_is_skipped_without_existing_context_or_after_content_failure(self):
        for bank, content_review in (([], review()), (self.distinct_bank(), review("grade_fit"))):
            with self.subTest(bank=bool(bank)):
                provider = ScriptedProvider(completion(candidate()), completion(content_review))
                result = await self.generator(provider, semantic_deduplication=True).generate(
                    self.request(existing_questions=bank)
                )
                self.assertEqual(result.metrics.calls, 2)
                self.assertEqual(provider.steps, [])
                self.assertEqual(result.status, "failed" if bank else "completed")
                if result.questions:
                    self.assertIsNone(result.questions[0].generation.distinctness_review)

    async def test_distinct_out_of_scope_failed_check_retries_instead_of_repairing_writer(self):
        for check in review().checks.model_dump():
            if check == "distinctness":
                continue
            with self.subTest(check=check):
                # A coherent Review can still be invalid for this narrower stage.
                mistaken = review(check)
                passed = review()
                provider = ScriptedProvider(
                    completion(candidate()),
                    completion(passed),
                    completion(mistaken),
                    completion(passed),
                )
                result = await self.generator(
                    provider,
                    semantic_deduplication=True,
                    attempts_per_stage=2,
                ).generate(self.request(existing_questions=self.distinct_bank()))
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.metrics.calls, 4)
                self.assertAlmostEqual(result.metrics.cost_usd, 0.04)
                self.assertEqual(result.questions[0].text, candidate().text)
                self.assertEqual(result.questions[0].generation.distinctness_review, passed)
                self.assertEqual(result.questions[0].generation.review, passed)
                self.assertEqual(provider.steps, [])
                original = payload(provider.calls[2])
                retry = payload(provider.calls[3])
                feedback = retry.pop("format_errors")
                self.assertTrue(feedback)
                self.assertIn("distinctness", json.dumps(feedback))
                self.assertEqual(retry, original)
                self.assertEqual(provider.calls[3]["model"], self.settings.reviewer.model)
                self.assertEqual(
                    provider.calls[3]["response_format"]["json_schema"]["name"], "Review"
                )
                self.assertEqual(provider.calls[2]["messages"][0], provider.calls[3]["messages"][0])

    async def test_distinct_prompt_excludes_common_rubric_instructions_and_explains_projection(
        self,
    ):
        provider = ScriptedProvider(
            completion(candidate()), completion(review()), completion(review())
        )
        result = await self.generator(provider, semantic_deduplication=True).generate(
            self.request(existing_questions=self.distinct_bank())
        )
        self.assertEqual(result.status, "completed")
        system = provider.calls[2]["messages"][0]["content"]
        self.assertIn(DISTINCT, system)
        self.assertNotIn(COMMON, system)
        self.assertNotIn("Ровно три anchors", system)
        self.assertIn("компактные проекции", system)
        self.assertIn("уже проверены другим этапом", system)
        self.assertIn("Не проверяй их наличие", system)
        for call in provider.calls[:2]:
            self.assertIn(COMMON, call["messages"][0]["content"])
        self.assertEqual(
            set(payload(provider.calls[2])["candidate"]), {"text", "skills", "criteria"}
        )
        self.assertEqual(result.questions[0].generation.prompt_version, VERSION)

    async def test_distinct_retry_is_explicit_and_cached_result_does_not_repeat_it(self):
        request = self.request(existing_questions=self.distinct_bank())
        provider = ScriptedProvider(
            completion(candidate()),
            completion(review()),
            completion("{broken"),
            completion(review()),
        )
        result = await self.generator(
            provider, semantic_deduplication=True, attempts_per_stage=2
        ).generate(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metrics.calls, 4)
        self.assertIn("format_errors", payload(provider.calls[3]))
        self.assertEqual(provider.calls[3]["model"], self.settings.reviewer.model)
        self.assertTrue(
            all(result.questions[0].generation.distinctness_review.checks.model_dump().values())
        )
        unused = ScriptedProvider()
        cached = await self.generator(
            unused, semantic_deduplication=True, attempts_per_stage=2
        ).generate(request)
        self.assertEqual(cached, result)
        self.assertEqual(unused.calls, [])

    def plan_fixture(self, count=3):
        return GenerationPlan(
            targets=[
                {
                    "intent": "Различить подсчёт строк и непустых значений COUNT.",
                    "boundaries": "Не обсуждать AVG, фильтрацию групп или соединения.",
                },
                {
                    "intent": "Различить этапы фильтрации WHERE и HAVING.",
                    "boundaries": "Не повторять подсчёт NULL или вычисление среднего.",
                },
                {
                    "intent": "Объяснить влияние NULL на среднее значение AVG.",
                    "boundaries": "Не повторять различия COUNT и фильтрации групп.",
                },
            ][:count]
        )

    def planned_candidates(self):
        average = candidate().model_dump()
        average["text"] = (
            "Как AVG(amount) в PostgreSQL обработает значения 10, NULL, 20 и пустую таблицу?"
        )
        average["skills"] = ["SQL: среднее значение и NULL"]
        descriptions = (
            "Понимание состава значений для среднего",
            "Понимание отсутствия наблюдений",
        )
        requirements = (
            "Исключает NULL из среднего и получает 15 для значений 10, NULL, 20.",
            "Указывает NULL для пустого набора и для набора только из NULL.",
        )
        for criterion, description, full in zip(
            average["criteria"], descriptions, requirements, strict=False
        ):
            criterion["description"] = description
            for config in criterion["grade_configs"]:
                config["anchors"][2]["description"] = full
        for ref in average["grade_configs"]:
            ref["reference_answer"] = (
                "AVG(amount) исключает NULL: (10 + 20) / 2 = 15. "
                "Для пустого набора или набора только из NULL возвращает NULL, а не ноль."
            )
        return [candidate(), self.distinct_repair(), Candidate.model_validate(average)]

    async def test_plan_three_targets_align_with_writer_review_and_metadata(self):
        plan = self.plan_fixture()
        candidates = self.planned_candidates()
        steps = [completion(plan)]
        for item in candidates:
            steps.extend([completion(item), completion(review())])
        provider = ScriptedProvider(*steps)
        request = self.request(count=3, comment="Три разных навыка SQL")
        result = await self.generator(provider, plan_slots=True).generate(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metrics.calls, 7)
        self.assertAlmostEqual(result.metrics.cost_usd, 0.07)
        self.assertEqual(provider.steps, [])
        planning = provider.calls[0]
        self.assertEqual(planning["max_tokens"], 4000)
        self.assertEqual(planning["model"], self.settings.writer.model)
        self.assertEqual(planning["response_format"]["json_schema"]["name"], "GenerationPlan")
        self.assertEqual(
            planning["response_format"]["json_schema"]["schema"], GenerationPlan.model_json_schema()
        )
        self.assertEqual(payload(planning)["count"], 3)
        self.assertEqual(payload(planning)["comment"], request["comment"])
        targets = plan.model_dump()["targets"]
        for index, (question, item, target) in enumerate(
            zip(result.questions, candidates, targets, strict=False)
        ):
            with self.subTest(slot=index):
                writer, reviewer = provider.calls[1 + index * 2 : 3 + index * 2]
                for call in (writer, reviewer):
                    self.assertEqual(payload(call)["target"], target)
                    self.assertEqual(payload(call)["all_targets"], targets)
                self.assertEqual(payload(reviewer)["candidate"], item.model_dump())
                self.assertEqual(reviewer["model"], self.settings.reviewer.model)
                self.assertEqual(question.text, item.text)
                self.assertEqual(question.generation.plan_target, target["intent"])
                self.assertEqual(question.generation.review, review())
        journal = json.loads((Path(self.temp.name) / (request["request_id"] + ".json")).read_text())
        self.assertEqual(journal["plan"], plan.model_dump())

    async def test_malformed_plan_retries_before_any_slot_writes(self):
        valid = self.plan_fixture()
        duplicate = valid.model_dump()
        duplicate["targets"][1]["intent"] = "  " + duplicate["targets"][0]["intent"].upper() + "!!!"
        for invalid in ("{broken", self.plan_fixture(1).model_dump_json(), json.dumps(duplicate)):
            with self.subTest(invalid=invalid[:60]):
                steps = [completion(invalid), completion(valid)]
                for item in self.planned_candidates():
                    steps.extend([completion(item), completion(review())])
                provider = ScriptedProvider(*steps)
                result = await self.generator(
                    provider, plan_slots=True, attempts_per_stage=2
                ).generate(self.request(count=3))
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.metrics.calls, 8)
                self.assertAlmostEqual(result.metrics.cost_usd, 0.08)
                self.assertEqual(
                    [c["response_format"]["json_schema"]["name"] for c in provider.calls[:3]],
                    ["GenerationPlan", "GenerationPlan", "Candidate"],
                )
                self.assertTrue(payload(provider.calls[1])["format_errors"])
                self.assertEqual(provider.calls[1]["max_tokens"], 4000)
                self.assertEqual(provider.calls[1]["model"], self.settings.fallback_writer.model)
                self.assertEqual(provider.steps, [])

    async def test_failed_plan_exhausts_two_attempts_without_slot_calls(self):
        provider = ScriptedProvider(completion("{broken"), completion('{"targets": []}'))
        request = self.request(count=3)
        result = await self.generator(
            provider, plan_slots=True, attempts_per_stage=2, max_rounds=3
        ).generate(request)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.questions, [])
        self.assertEqual(result.rejected_candidates, [])
        self.assertIn("planning_failed", self.codes(result))
        self.assertIn("count_shortfall", self.codes(result))
        self.assertEqual(result.metrics.calls, 2)
        self.assertAlmostEqual(result.metrics.cost_usd, 0.02)
        self.assertEqual(
            [c["response_format"]["json_schema"]["name"] for c in provider.calls],
            ["GenerationPlan", "GenerationPlan"],
        )
        unused = ScriptedProvider()
        cached = await self.generator(
            unused, plan_slots=True, attempts_per_stage=2, max_rounds=3
        ).generate(request)
        self.assertEqual(cached, result)
        self.assertEqual(unused.calls, [])

    async def test_plan_response_and_saved_plan_replay_without_another_planner_call(self):
        for crash_point in ("response", "plan"):
            with self.subTest(crash_point=crash_point):
                request = self.request()
                plan = self.plan_fixture(1)
                provider = ScriptedProvider(completion(plan))
                save = Journal.save

                def crash_after_save(journal):
                    save(journal)
                    paid = any(c.get("response") for c in journal.data["calls"].values())
                    if (crash_point == "response" and paid) or (
                        crash_point == "plan" and journal.data.get("plan")
                    ):
                        raise Crash()

                with patch.object(Journal, "save", crash_after_save):
                    with self.assertRaises(Crash):
                        await self.generator(provider, plan_slots=True).generate(request)
                self.assertEqual(len(provider.calls), 1)
                resumed = ScriptedProvider(completion(candidate()), completion(review()))
                result = await self.generator(resumed, plan_slots=True).generate(request)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.metrics.calls, 3)
                self.assertAlmostEqual(result.metrics.cost_usd, 0.03)
                self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
                self.assertEqual(
                    [c["response_format"]["json_schema"]["name"] for c in resumed.calls],
                    ["Candidate", "Review"],
                )
                self.assertEqual(payload(resumed.calls[0])["target"], plan.targets[0].model_dump())
                self.assertEqual(result.questions[0].generation.plan_target, plan.targets[0].intent)

    async def test_production_default_policies_work_with_explicit_offline_responses(self):
        defaults = GenerationSettings()
        provider = ScriptedProvider(
            completion(self.plan_fixture(1)), completion(candidate()), completion(review())
        )
        generator = QuestionGenerator(provider, state_dir=self.temp.name)
        result = await generator.generate(self.request())
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metrics.calls, 3)
        self.assertEqual(generator.settings, defaults)
        for call, policy in zip(
            provider.calls, (defaults.writer, defaults.writer, defaults.reviewer), strict=False
        ):
            self.assertEqual(call["model"], policy.model)
            self.assertEqual(
                call["provider"]["max_price"],
                {
                    "prompt": policy.input_usd_per_million,
                    "completion": policy.output_usd_per_million,
                },
            )
        self.assertEqual(result.questions[0].generation.model, defaults.writer.model)
        self.assertEqual(result.questions[0].generation.reviewer_model, defaults.reviewer.model)
        self.assertIsNone(result.questions[0].generation.distinctness_review)
        self.assertEqual(provider.steps, [])
        self.assertEqual(GenerationRequest(**self.request(count=10)).count, 10)

    async def test_mutated_request_instance_is_revalidated_before_paid_call(self):
        request = GenerationRequest(**self.request())
        request.count = 100
        provider = ScriptedProvider()
        with self.assertRaises(ValidationError):
            await self.generator(provider).generate(request)
        self.assertEqual(provider.calls, [])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_actual_cost_overrun_on_final_review_warns_and_stops_new_calls(self):
        for count in (1, 2):
            with self.subTest(count=count):
                provider = ScriptedProvider(completion(candidate()), completion(review(), cost=1.0))
                result = await self.generator(provider, max_cost_usd=0.75).generate(
                    self.request(count=count)
                )
                self.assertEqual(result.status, "completed" if count == 1 else "partial")
                self.assertEqual(len(result.questions), 1)
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(result.metrics.calls, 2)
                self.assertAlmostEqual(result.metrics.cost_usd, 1.01)
                self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
                self.assertIn("cost_overrun", self.codes(result))
                if count == 2:
                    self.assertIn("budget_exhausted", self.codes(result))

    async def test_source_material_and_bank_completeness_warnings(self):
        for complete in (False, True):
            for source in ("absent", "provided", "catalog"):
                with self.subTest(complete=complete, source=source):
                    provider = ScriptedProvider(completion(candidate()), completion(review()))
                    request = self.request(
                        area="SQL" if source == "catalog" else "Неизвестная область",
                        bank_context_complete=complete,
                        materials=[{"id": "manual", "text": "COUNT исключает NULL."}]
                        if source == "provided"
                        else [],
                    )
                    result = await self.generator(provider).generate(request)
                    self.assertEqual(result.status, "completed")
                    self.assertEqual("bank_context_incomplete" in self.codes(result), not complete)
                    self.assertEqual(
                        "no_source_materials" in self.codes(result), source == "absent"
                    )
                    self.assertEqual(
                        bool(payload(provider.calls[0])["materials"]), source != "absent"
                    )
                    if source == "provided":
                        self.assertEqual(result.questions[0].generation.material_ids, ["manual"])

    async def test_minimum_grade_covers_all_higher_grades(self):
        for grade in GRADES:
            with self.subTest(grade=grade):
                provider = ScriptedProvider(completion(candidate(grade)), completion(review()))
                request = GenerationRequest(**self.request(grade=grade, comment="Проверить NULL"))
                result = await self.generator(provider).generate(request)
                self.assertEqual(result.status, "completed")
                self.assertEqual(len(result.questions), 1)
                question = result.questions[0]
                expected = GRADES[GRADES.index(grade) :]
                self.assertEqual(question.min_grade, grade)
                self.assertEqual(question.grades, expected)
                self.assertEqual([g.grade for g in question.grade_configs], expected)
                for criterion in question.criteria:
                    self.assertEqual([g.grade for g in criterion.grade_configs], expected)
                self.assertEqual(question.status, "draft")
                self.assertEqual(question.generation.comment, request.comment)
                self.assertEqual(question.generation.prompt_version, VERSION)
                self.assertEqual(question.generation.model, self.settings.writer.model)
                self.assertEqual(question.generation.reviewer_model, self.settings.reviewer.model)
                for call, policy in zip(
                    provider.calls, (self.settings.writer, self.settings.reviewer), strict=False
                ):
                    self.assertEqual(call["model"], policy.model)
                    self.assertEqual(
                        call["provider"]["max_price"],
                        {
                            "prompt": policy.input_usd_per_million,
                            "completion": policy.output_usd_per_million,
                        },
                    )
                self.assertTrue(all(question.generation.review.checks.model_dump().values()))
                self.assertEqual(result.rejected_candidates, [])
                self.assertEqual(result.metrics.calls, 2)
                self.assertAlmostEqual(result.metrics.cost_usd, 0.02)
                self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
                self.assertNotEqual(
                    provider.calls[0]["model"].split("/")[0],
                    provider.calls[1]["model"].split("/")[0],
                )

    async def test_overlapping_writer_anchors_compiled_before_review_with_raw_response_preserved(
        self,
    ):
        raw = candidate("Junior").model_dump()
        for criterion in raw["criteria"]:
            for config in criterion["grade_configs"]:
                # Both authored bands include a partially correct answer.
                config["anchors"][0]["description"] = (
                    "Ответ неверен или раскрывает только часть требований."
                )
                config["anchors"][1]["description"] = (
                    "Ответ раскрывает часть требований, но содержит пропуски."
                )
        authored = Candidate.model_validate(raw)
        writer_response = completion(authored)
        provider = ScriptedProvider(writer_response, completion(review()))
        request = self.request(grade="Junior")
        result = await self.generator(provider).generate(request)

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[1]["response_format"]["json_schema"]["name"], "Review")
        reviewed = payload(provider.calls[1])["candidate"]
        expected = copy.deepcopy(raw)
        for criterion in expected["criteria"]:
            for config in criterion["grade_configs"]:
                config["anchors"][0]["description"] = ZERO
                config["anchors"][1]["description"] = PARTIAL
        # Full comparison ensures text, references, weights, and especially every
        # model-authored score-2 requirement survive compilation unchanged.
        self.assertEqual(reviewed, expected)
        accepted = result.questions[0].model_dump(include=set(Candidate.model_fields))
        self.assertEqual(accepted, reviewed)
        self.assertEqual(authored.model_dump(), raw)

        journal = json.loads((Path(self.temp.name) / (request["request_id"] + ".json")).read_text())
        writer_calls = [call for call in journal["calls"].values() if call["stage"] == "write"]
        self.assertEqual(len(writer_calls), 1)
        self.assertEqual(writer_calls[0]["response"]["content"], writer_response.content)
        self.assertEqual(json.loads(writer_calls[0]["response"]["content"]), raw)
        self.assertNotEqual(reviewed, raw)

    async def test_wrong_grade_coverage_is_not_sent_to_review(self):
        provider = ScriptedProvider(completion(candidate("Senior")))
        result = await self.generator(provider).generate(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.questions, [])
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("quality_attempts_exhausted", self.codes(result))

    async def test_partial_batch_salvages_accepted_and_unverified_candidates(self):
        second = candidate(
            text="Как PostgreSQL вычислит COUNT(email) на пустой таблице пользователей?"
        )
        provider = ScriptedProvider(
            completion(candidate()), completion(review()), completion(second), completion("{broken")
        )
        result = await self.generator(provider).generate(self.request(count=2))
        self.assertEqual(result.status, "partial")
        self.assertEqual([q.text for q in result.questions], [candidate().text])
        rejected = result.rejected_candidates[0]
        self.assertEqual(rejected.candidate, second)
        self.assertEqual(rejected.slot, 1)
        self.assertEqual(rejected.quality_status, "unverified")
        self.assertIn("count_shortfall", self.codes(result))
        self.assertEqual(result.metrics.calls, 4)
        self.assertEqual(
            payload(provider.calls[2])["existing_questions"][-1]["text"], candidate().text
        )

    async def test_each_independent_review_failure_requires_repair_and_new_review(self):
        for check in review().checks.model_dump():
            with self.subTest(check=check):
                failed = review(check)
                repaired = candidate(
                    text="Как COUNT(*) и COUNT(email) в PostgreSQL обрабатывают NULL и пустую таблицу?"
                )
                provider = ScriptedProvider(
                    completion(candidate()),
                    completion(failed),
                    completion(repaired),
                    completion(review()),
                )
                result = await self.generator(provider, max_rounds=2).generate(self.request())
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.questions[0].text, repaired.text)
                self.assertEqual(result.metrics.calls, 4)
                self.assertEqual(provider.calls[2]["model"], self.settings.writer.model)
                repair_data = payload(provider.calls[2])
                self.assertEqual(repair_data["previous_candidate"], candidate().model_dump())
                self.assertEqual(repair_data["feedback"], failed.model_dump())
                self.assertEqual(payload(provider.calls[3])["candidate"], repaired.model_dump())
                self.assertEqual(result.questions[0].generation.model, self.settings.writer.model)

    def atomic_filter_candidate(self):
        value = self.distinct_repair().model_dump()
        value["criteria"] = value["criteria"][:1]
        criterion = value["criteria"][0]
        criterion.update(
            id="filter_stage", description="Различение этапов фильтрации строк и групп"
        )
        for config in criterion["grade_configs"]:
            config["anchors"][2]["description"] = (
                "Объясняет WHERE до группировки и HAVING после группировки, "
                "включая возможность условия на агрегат в HAVING."
            )
        return Candidate.model_validate(value)

    async def test_single_criterion_atomic_task_is_reviewed_and_accepted(self):
        atomic = self.atomic_filter_candidate()
        provider = ScriptedProvider(completion(atomic), completion(review()))
        result = await self.generator(provider).generate(self.request())
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metrics.calls, 2)
        self.assertEqual(len(result.questions[0].criteria), 1)
        self.assertEqual(result.questions[0].criteria, atomic.criteria)
        self.assertEqual(payload(provider.calls[1])["candidate"], atomic.model_dump())
        schema = provider.calls[0]["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["criteria"]["minItems"], 1)
        self.assertEqual(schema["properties"]["criteria"]["maxItems"], 4)
        for size in (0, 5):
            invalid = atomic.model_dump()
            invalid["criteria"] = invalid["criteria"] * size
            with self.subTest(size=size), self.assertRaises(ValidationError):
                Candidate.model_validate(invalid)

    async def test_rubric_and_reference_failures_preserve_task_in_third_round(self):
        atomic = self.atomic_filter_candidate()
        for check in ("rubric_alignment", "reference_completeness"):
            for failures in (2, 3):
                with self.subTest(check=check, failures=failures):
                    failed = review(check)
                    steps = []
                    for round_index in range(3):
                        steps.extend(
                            [
                                completion(atomic),
                                completion(failed if round_index < failures else review()),
                            ]
                        )
                    provider = ScriptedProvider(*steps)
                    result = await self.generator(provider, max_rounds=3).generate(self.request())
                    self.assertEqual(result.metrics.calls, 6)
                    self.assertEqual(provider.steps, [])
                    for call in (provider.calls[2], provider.calls[4]):
                        data = payload(call)
                        self.assertEqual(data["repair_mode"], "repair")
                        self.assertEqual(data["previous_candidate"], atomic.model_dump())
                        self.assertEqual(data["feedback"], failed.model_dump())
                        self.assertEqual(call["model"], self.settings.writer.model)
                    self.assertEqual(payload(provider.calls[5])["candidate"], atomic.model_dump())
                    if failures == 2:
                        self.assertEqual(result.status, "completed")
                        self.assertEqual(result.questions[0].text, atomic.text)
                        self.assertEqual(result.questions[0].criteria, atomic.criteria)
                    else:
                        self.assertEqual(result.status, "failed")
                        self.assertEqual(result.questions, [])
                        self.assertEqual(result.rejected_candidates[0].candidate, atomic)
                        self.assertEqual(result.rejected_candidates[0].issues, failed.defects)

    async def test_task_level_failures_still_request_third_round_redesign(self):
        for check in ("factual_correctness", "scope_alignment", "grade_fit", "distinctness"):
            with self.subTest(check=check):
                failed = review(check)
                replacement = self.atomic_filter_candidate()
                provider = ScriptedProvider(
                    completion(candidate()),
                    completion(failed),
                    completion(candidate()),
                    completion(failed),
                    completion(replacement),
                    completion(review()),
                )
                result = await self.generator(provider, max_rounds=3).generate(self.request())
                self.assertEqual(result.status, "completed")
                self.assertEqual(payload(provider.calls[2])["repair_mode"], "repair")
                self.assertEqual(payload(provider.calls[4])["repair_mode"], "redesign")
                self.assertEqual(
                    payload(provider.calls[4])["previous_candidate"], candidate().model_dump()
                )
                self.assertEqual(payload(provider.calls[4])["feedback"], failed.model_dump())
                self.assertEqual(result.questions[0].text, replacement.text)
                self.assertEqual(result.metrics.calls, 6)

    async def test_repeated_dedicated_distinct_rejection_still_redesigns(self):
        duplicate = self.duplicate_review()
        replacement = self.atomic_filter_candidate()
        provider = ScriptedProvider(
            completion(candidate()),
            completion(review()),
            completion(duplicate),
            completion(candidate()),
            completion(review()),
            completion(duplicate),
            completion(replacement),
            completion(review()),
            completion(review()),
        )
        result = await self.generator(provider, max_rounds=3, semantic_deduplication=True).generate(
            self.request(existing_questions=self.distinct_bank())
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(payload(provider.calls[3])["repair_mode"], "repair")
        self.assertEqual(payload(provider.calls[6])["repair_mode"], "redesign")
        self.assertEqual(payload(provider.calls[6])["feedback"], duplicate.model_dump())
        self.assertEqual(result.questions[0].text, replacement.text)
        self.assertEqual(result.questions[0].generation.distinctness_review, review())
        self.assertEqual(result.metrics.calls, 9)
        self.assertEqual(provider.steps, [])

    def test_overlong_model_ids_rejected_by_settings_before_generation(self):
        defaults = self.settings.model_dump()
        for name in ("writer", "fallback_writer", "reviewer"):
            policy = defaults[name]
            family = policy["model"].split("/")[0] + "/"
            for length in (129, 130):
                invalid = copy.deepcopy(defaults)
                invalid[name]["model"] = family + "x" * (length - len(family))
                with self.subTest(policy=name, length=length), self.assertRaises(ValidationError):
                    GenerationSettings.model_validate(invalid)
            boundary = {**policy, "model": family + "x" * (128 - len(family))}
            self.assertEqual(len(ModelPolicy.model_validate(boundary).model), 128)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    async def test_error_completion_retries_with_known_cost_accounted(self):
        provider = ScriptedProvider(
            completion("", cost=0.04, finish="error"), completion(candidate()), completion(review())
        )
        request = self.request()
        result = await self.generator(provider, attempts_per_stage=2).generate(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.metrics.calls, 3)
        self.assertAlmostEqual(result.metrics.cost_usd, 0.06)
        self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
        self.assertNotIn("cost_uncertain", self.codes(result))
        self.assertEqual(provider.calls[1]["model"], self.settings.fallback_writer.model)
        self.assertIn("error", json.dumps(payload(provider.calls[1])["format_errors"]))
        journal = json.loads((Path(self.temp.name) / (request["request_id"] + ".json")).read_text())
        first = next(iter(journal["calls"].values()))
        self.assertEqual(first["response"]["finish_reason"], "error")
        self.assertEqual(first["response"]["cost_usd"], 0.04)

    async def test_deadline_or_cancel_after_durable_intent_prevents_dispatch(self):
        for cause in ("deadline", "cancel"):
            with self.subTest(cause=cause):
                event = asyncio.Event()
                expired = False
                save = Journal.save
                monotonic = time.monotonic

                def clock():
                    return monotonic() + (self.settings.deadline_seconds + 1 if expired else 0)

                def save_then_stop(journal):
                    nonlocal expired
                    save(journal)
                    if any(c["status"] == "pending" for c in journal.data["calls"].values()):
                        if cause == "deadline":
                            expired = True
                        else:
                            event.set()

                provider = ScriptedProvider()
                request = self.request()
                with (
                    patch.object(Journal, "save", save_then_stop),
                    patch("app.question_generation.service.time.monotonic", side_effect=clock),
                ):
                    result = await self.generator(provider).generate(request, cancel_event=event)
                self.assertEqual(result.status, "cancelled" if cause == "cancel" else "failed")
                self.assertIn(
                    "cancelled" if cause == "cancel" else "deadline_exceeded", self.codes(result)
                )
                self.assertEqual(provider.calls, [])
                self.assertEqual(result.metrics.calls, 0)
                self.assertEqual(result.metrics.cost_usd, 0)
                self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
                journal = json.loads(
                    (Path(self.temp.name) / (request["request_id"] + ".json")).read_text()
                )
                self.assertEqual(len(journal["calls"]), 1)
                intent = next(iter(journal["calls"].values()))
                self.assertEqual(intent["status"], "failed")
                self.assertEqual(intent["error"], "not_dispatched")
                self.assertIs(intent["dispatched"], False)

    async def test_failed_review_never_escapes_as_accepted_draft(self):
        failed = review("factual_correctness")
        provider = ScriptedProvider(completion(candidate()), completion(failed))
        result = await self.generator(provider).generate(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.questions, [])
        self.assertEqual(result.rejected_candidates[0].quality_status, "needs_revision")
        self.assertEqual(result.rejected_candidates[0].issues, failed.defects)

    async def test_malformed_or_truncated_writer_response_retries_with_feedback(self):
        for bad in [
            completion("{invalid"),
            completion("[]"),
            completion(candidate(), finish="length"),
        ]:
            with self.subTest(content=bad.content[:20], finish=bad.finish_reason):
                provider = ScriptedProvider(bad, completion(candidate()), completion(review()))
                result = await self.generator(provider, attempts_per_stage=2).generate(
                    self.request()
                )
                self.assertEqual(result.status, "completed")
                self.assertIn("format_errors", payload(provider.calls[1]))
                self.assertEqual(provider.calls[1]["model"], self.settings.fallback_writer.model)
                self.assertEqual(
                    provider.calls[1]["provider"]["max_price"],
                    {
                        "prompt": self.settings.fallback_writer.input_usd_per_million,
                        "completion": self.settings.fallback_writer.output_usd_per_million,
                    },
                )
                self.assertEqual(result.metrics.calls, 3)
                self.assertAlmostEqual(result.metrics.cost_usd, 0.03)

    async def test_incoherent_review_is_retried_not_accepted(self):
        invalid = review().model_dump()
        invalid["checks"]["distinctness"] = False
        provider = ScriptedProvider(
            completion(candidate()), completion(json.dumps(invalid)), completion(review())
        )
        result = await self.generator(provider, attempts_per_stage=2).generate(self.request())
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(provider.calls), 3)
        self.assertIn("format_errors", payload(provider.calls[2]))
        self.assertEqual(provider.calls[1]["model"], provider.calls[2]["model"])

    async def test_retry_exhaustion_is_bounded_across_rounds(self):
        provider = ScriptedProvider(
            *[ProviderFailure("http_429", no_charge=True) for _ in range(4)]
        )
        result = await self.generator(provider, attempts_per_stage=2, max_rounds=2).generate(
            self.request()
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.metrics.calls, 4)
        self.assertEqual(result.metrics.cost_usd, 0)
        self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
        self.assertIn("quality_attempts_exhausted", self.codes(result))
        self.assertEqual(provider.steps, [])

    async def test_auth_failure_stops_without_retry_or_charge(self):
        provider = ScriptedProvider(ProviderFailure("http_401", retryable=False, no_charge=True))
        result = await self.generator(provider, attempts_per_stage=3, max_rounds=3).generate(
            self.request(count=3)
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.metrics.calls, 1)
        self.assertEqual(result.metrics.cost_usd, 0)
        self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
        self.assertIn("http_401", self.codes(result))

    async def test_unknown_billing_is_reserved_even_for_valid_accepted_response(self):
        provider = ScriptedProvider(completion(candidate(), cost=None), completion(review()))
        result = await self.generator(provider).generate(self.request())
        self.assertEqual(result.status, "completed")
        self.assertAlmostEqual(result.metrics.cost_usd, 0.01)
        self.assertGreater(result.metrics.reserved_uncertain_usd, 0)
        self.assertIn("cost_uncertain", self.codes(result))

    async def test_budget_gate_runs_before_first_provider_call(self):
        provider = ScriptedProvider()
        result = await self.generator(provider, max_cost_usd=0.000001).generate(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(provider.calls, [])
        self.assertEqual(result.metrics.calls, 0)
        self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
        self.assertIn("budget_exhausted", self.codes(result))

    async def test_known_spend_blocks_review_and_salvages_candidate(self):
        provider = ScriptedProvider(completion(candidate(), cost=0.1))
        result = await self.generator(provider, max_cost_usd=0.1).generate(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.rejected_candidates[0].candidate, candidate())
        self.assertEqual(result.rejected_candidates[0].quality_status, "unverified")
        self.assertIn("budget_exhausted", self.codes(result))

    async def test_uncertain_failure_reservation_blocks_next_attempt(self):
        # Measure the public reservation, then allow one ceiling but not two.
        first = await self.generator(
            ScriptedProvider(ProviderFailure("timeout_uncertain"))
        ).generate(self.request())
        reserve = first.metrics.reserved_uncertain_usd
        self.assertGreater(reserve, 0)
        provider = ScriptedProvider(ProviderFailure("timeout_uncertain"))
        result = await self.generator(
            provider, attempts_per_stage=2, max_cost_usd=reserve * 1.5
        ).generate(self.request())
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.metrics.reserved_uncertain_usd, reserve)
        self.assertIn("budget_exhausted", self.codes(result))

    async def test_existing_duplicate_is_rejected_before_review(self):
        old = {
            "id": "existing",
            "area": "SQL",
            "text": "  " + candidate().text.upper().replace("?", " !!! "),
        }
        provider = ScriptedProvider(completion(candidate()))
        result = await self.generator(provider).generate(self.request(existing_questions=[old]))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.questions, [])
        self.assertEqual(len(provider.calls), 1)

    async def test_duplicate_of_earlier_batch_question_is_not_accepted(self):
        provider = ScriptedProvider(
            completion(candidate()), completion(review()), completion(candidate())
        )
        result = await self.generator(provider).generate(self.request(count=2))
        self.assertEqual(result.status, "partial")
        self.assertEqual(len(result.questions), 1)
        self.assertEqual(len(provider.calls), 3)

    async def test_idempotence_survives_new_service_instance_without_provider_calls(self):
        request = self.request()
        first = await self.generator(
            ScriptedProvider(completion(candidate()), completion(review()))
        ).generate(request)
        unused = ScriptedProvider()
        second = await self.generator(unused).generate(request)
        self.assertEqual(second, first)
        self.assertEqual(unused.calls, [])

    async def test_idempotency_conflict_for_changed_request_or_settings(self):
        request = self.request()
        await self.generator(
            ScriptedProvider(completion(candidate()), completion(review()))
        ).generate(request)
        unused = ScriptedProvider()
        with self.assertRaises(IdempotencyConflict):
            await self.generator(unused).generate({**request, "comment": "Иной запрос"})
        with self.assertRaises(IdempotencyConflict):
            await self.generator(unused, max_rounds=2).generate(request)
        self.assertEqual(unused.calls, [])

    async def test_concurrent_same_job_is_locked_then_cached(self):
        blocked = BlockedCall()
        provider = ScriptedProvider(completion(candidate()), blocked)
        request = self.request()
        task = asyncio.create_task(self.generator(provider).generate(request))
        try:
            await asyncio.wait_for(blocked.started.wait(), 2)
            other = ScriptedProvider()
            with self.assertRaises(JobBusy):
                await self.generator(other).generate(request)
            self.assertEqual(other.calls, [])
            blocked.release.set()
            first = await asyncio.wait_for(task, 2)
            self.assertEqual(await self.generator(other).generate(request), first)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_event_midflight_cleans_up_and_preserves_candidate(self):
        blocked = BlockedCall()
        provider = ScriptedProvider(completion(candidate()), blocked)
        event = asyncio.Event()
        request = self.request()
        task = asyncio.create_task(self.generator(provider).generate(request, cancel_event=event))
        try:
            await asyncio.wait_for(blocked.started.wait(), 2)
            event.set()
            result = await asyncio.wait_for(task, 2)
            self.assertEqual(result.status, "cancelled")
            self.assertTrue(blocked.finished.is_set())
            self.assertEqual(result.questions, [])
            self.assertEqual(result.rejected_candidates[0].candidate, candidate())
            self.assertEqual(result.rejected_candidates[0].quality_status, "unverified")
            self.assertGreater(result.metrics.reserved_uncertain_usd, 0)
            self.assertEqual(result.metrics.calls, 2)
            self.assertEqual(await self.generator(ScriptedProvider()).generate(request), result)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_pre_cancelled_event_makes_no_calls(self):
        event = asyncio.Event()
        event.set()
        provider = ScriptedProvider()
        result = await self.generator(provider).generate(self.request(), cancel_event=event)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.metrics.calls, 0)
        self.assertEqual(provider.calls, [])

    async def test_task_cancellation_propagates_and_restart_reuses_paid_writer(self):
        blocked = BlockedCall()
        request = self.request()
        provider = ScriptedProvider(completion(candidate()), blocked)
        task = asyncio.create_task(self.generator(provider, attempts_per_stage=2).generate(request))
        try:
            await asyncio.wait_for(blocked.started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(blocked.finished.is_set())
            resumed = ScriptedProvider(completion(review()))
            result = await self.generator(resumed, attempts_per_stage=2).generate(request)
            self.assertEqual(result.status, "completed")
            self.assertEqual(len(resumed.calls), 1)
            self.assertEqual(resumed.calls[0]["response_format"]["json_schema"]["name"], "Review")
            self.assertEqual(result.metrics.calls, 3)
            self.assertAlmostEqual(result.metrics.cost_usd, 0.02)
            self.assertGreater(result.metrics.reserved_uncertain_usd, 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_crash_during_write_consumes_attempt_and_reserves_uncertain_charge(self):
        request = self.request()
        with self.assertRaises(Crash):
            await self.generator(ScriptedProvider(Crash()), attempts_per_stage=2).generate(request)
        resumed = ScriptedProvider(completion(candidate()), completion(review()))
        result = await self.generator(resumed, attempts_per_stage=2).generate(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(resumed.calls), 2)
        self.assertEqual(resumed.calls[0]["model"], self.settings.fallback_writer.model)
        self.assertEqual(result.metrics.calls, 3)
        self.assertGreater(result.metrics.reserved_uncertain_usd, 0)

    async def test_crash_replay_of_repair_format_attempts_preserves_paid_work(self):
        request = self.request()
        repaired = candidate(
            text="Как в PostgreSQL подсчитать строки и непустые email, включая пустую таблицу?"
        )
        provider = ScriptedProvider(
            completion(candidate()),
            completion(review("rubric_alignment")),
            completion("{bad repair"),
            completion(repaired),
            Crash(),
        )
        with self.assertRaises(Crash):
            await self.generator(provider, max_rounds=2, attempts_per_stage=2).generate(request)
        self.assertEqual(provider.calls[2]["model"], self.settings.writer.model)
        self.assertEqual(provider.calls[3]["model"], self.settings.fallback_writer.model)
        resumed = ScriptedProvider(completion(review()))
        result = await self.generator(resumed, max_rounds=2, attempts_per_stage=2).generate(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.questions[0].text, repaired.text)
        self.assertEqual(len(resumed.calls), 1)
        self.assertEqual(payload(resumed.calls[0])["candidate"], repaired.model_dump())
        self.assertEqual(result.metrics.calls, 6)
        self.assertAlmostEqual(result.metrics.cost_usd, 0.05)
        self.assertGreater(result.metrics.reserved_uncertain_usd, 0)

    async def test_restart_keeps_accepted_slots_and_their_duplicate_context(self):
        request = self.request(count=2)
        second = candidate(text="Что вернёт COUNT(email) для пустой таблицы в PostgreSQL?")
        initial = ScriptedProvider(completion(candidate()), completion(review()), Crash())
        with self.assertRaises(Crash):
            await self.generator(initial, attempts_per_stage=2).generate(request)
        resumed = ScriptedProvider(completion(second), completion(review()))
        result = await self.generator(resumed, attempts_per_stage=2).generate(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual([q.text for q in result.questions], [candidate().text, second.text])
        self.assertEqual(len({q.id for q in result.questions}), 2)
        self.assertEqual(len(resumed.calls), 2)
        self.assertEqual(
            payload(resumed.calls[0])["existing_questions"][-1]["id"], result.questions[0].id
        )
        self.assertEqual(result.metrics.calls, 5)
        self.assertAlmostEqual(result.metrics.cost_usd, 0.04)

    async def test_restart_does_not_reset_exhausted_attempt_limit(self):
        request = self.request()
        with self.assertRaises(Crash):
            await self.generator(ScriptedProvider(Crash())).generate(request)
        unused = ScriptedProvider()
        result = await self.generator(unused).generate(request)
        self.assertEqual(result.status, "failed")
        self.assertEqual(unused.calls, [])
        self.assertEqual(result.metrics.calls, 1)
        self.assertGreater(result.metrics.reserved_uncertain_usd, 0)
        self.assertIn("quality_attempts_exhausted", self.codes(result))

    async def test_repeated_review_rejection_exhausts_repairs_and_returns_latest_candidate(self):
        repaired = candidate(text="Как COUNT(email) работает с NULL в таблице PostgreSQL?")
        failed = review("reference_completeness")
        provider = ScriptedProvider(
            completion(candidate()), completion(failed), completion(repaired), completion(failed)
        )
        result = await self.generator(provider, max_rounds=2).generate(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.questions, [])
        self.assertEqual(len(result.rejected_candidates), 1)
        self.assertEqual(result.rejected_candidates[0].candidate, repaired)
        self.assertEqual(result.rejected_candidates[0].issues, failed.defects)
        self.assertEqual(result.metrics.calls, 4)
        self.assertIn("quality_attempts_exhausted", self.codes(result))

    async def test_omitting_request_id_creates_independent_jobs(self):
        request = self.request(request_id=None)
        provider = ScriptedProvider(
            completion(candidate()),
            completion(review()),
            completion(candidate()),
            completion(review()),
        )
        generator = self.generator(provider)
        first = await generator.generate(request)
        second = await generator.generate(request)
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertNotEqual(first.job_id, second.job_id)
        self.assertNotEqual(first.questions[0].id, second.questions[0].id)
        self.assertEqual(len(provider.calls), 4)

    async def test_paid_response_replay_after_deadline_or_budget_exhaustion(self):
        # Stop after durable response persistence, before its consumer runs. This
        # exercises response replay, not the shortcut for a stored final result.
        for limit in ("deadline", "budget"):
            for response_count in (1, 2, 4):
                with self.subTest(limit=limit, responses=response_count):
                    request = self.request()
                    steps = [completion(candidate()), completion(review())]
                    if response_count == 4:
                        steps = [
                            completion(candidate()),
                            completion(review("grade_fit")),
                            completion(candidate()),
                            completion(review()),
                        ]
                    steps = steps[:response_count]
                    if limit == "budget":
                        last = steps[-1]
                        steps[-1] = completion(
                            last.content, cost=0.75 - 0.01 * (response_count - 1)
                        )
                    provider = ScriptedProvider(*steps)
                    save = Journal.save

                    def crash_after_response(journal):
                        save(journal)
                        paid = [c for c in journal.data["calls"].values() if c.get("response")]
                        if len(paid) == response_count:
                            raise Crash()

                    with patch.object(Journal, "save", crash_after_response):
                        with self.assertRaises(Crash):
                            await self.generator(provider, max_rounds=2).generate(request)
                    unused = ScriptedProvider()
                    future = (
                        time.time() + self.settings.deadline_seconds + 1
                        if limit == "deadline"
                        else time.time()
                    )
                    with patch("app.question_generation.service.time.time", return_value=future):
                        result = await self.generator(unused, max_rounds=2).generate(request)
                    self.assertEqual(unused.calls, [])
                    self.assertEqual(result.metrics.calls, response_count)
                    self.assertAlmostEqual(
                        result.metrics.cost_usd,
                        0.75 if limit == "budget" else 0.01 * response_count,
                    )
                    self.assertEqual(result.metrics.reserved_uncertain_usd, 0)
                    if response_count == 1:
                        self.assertEqual(result.status, "failed")
                        self.assertEqual(result.rejected_candidates[0].candidate, candidate())
                        self.assertEqual(result.rejected_candidates[0].quality_status, "unverified")
                        self.assertIn(
                            "deadline_exceeded" if limit == "deadline" else "budget_exhausted",
                            self.codes(result),
                        )
                    else:
                        self.assertEqual(result.status, "completed")
                        self.assertEqual(result.questions[0].text, candidate().text)
                        self.assertEqual(result.rejected_candidates, [])

    async def test_knowledge_adapter_preserves_grade_keys_and_draft_only_schema(self):
        for minimum in GRADES:
            with self.subTest(minimum=minimum):
                result = await self.generator(
                    ScriptedProvider(completion(candidate(minimum)), completion(review()))
                ).generate(self.request(grade=minimum))
                q = result.questions[0]
                rows = knowledge_rows(q)
                self.assertEqual(rows, knowledge_rows(q.model_dump()))
                self.assertEqual(
                    set(rows),
                    {
                        "questions",
                        "criteria",
                        "criterion_grade_configs",
                        "criterion_grade_anchors",
                        "question_grade_configs",
                    },
                )
                code = "gen_" + q.id.replace("-", "_")
                self.assertEqual(
                    rows["questions"],
                    [
                        {
                            "code": code,
                            "specialization": "analyst",
                            "topic": "SQL",
                            "question_text": q.text,
                            "min_grade": GRADES.index(minimum) + 1,
                            "version": 1,
                            "is_active": False,
                        }
                    ],
                )
                self.assertEqual(
                    rows["criteria"],
                    [
                        {
                            "question_code": code,
                            "code": c.id,
                            "description": c.description,
                            "sort_order": index,
                        }
                        for index, c in enumerate(q.criteria)
                    ],
                )
                self.assertEqual(
                    rows["criterion_grade_configs"],
                    [
                        {
                            "question_code": code,
                            "criterion_code": c.id,
                            "grade": GRADES.index(g.grade) + 1,
                            "weight": g.weight,
                        }
                        for c in q.criteria
                        for g in c.grade_configs
                    ],
                )
                self.assertEqual(
                    rows["criterion_grade_anchors"],
                    [
                        {
                            "question_code": code,
                            "criterion_code": c.id,
                            "grade": GRADES.index(g.grade) + 1,
                            **a.model_dump(),
                        }
                        for c in q.criteria
                        for g in c.grade_configs
                        for a in g.anchors
                    ],
                )
                self.assertEqual(
                    rows["question_grade_configs"],
                    [
                        {
                            "question_code": code,
                            "grade": GRADES.index(g.grade) + 1,
                            "reference_answer": g.reference_answer,
                        }
                        for g in q.grade_configs
                    ],
                )

    async def test_runtime_adapter_selects_grade_and_does_not_leak_answers_to_interviewer(self):
        result = await self.generator(
            ScriptedProvider(completion(candidate("Junior")), completion(review()))
        ).generate(self.request(grade="Junior"))
        draft = result.questions[0].model_dump()
        # Distinct per-grade values catch accidentally always selecting the first grade.
        for ref in draft["grade_configs"]:
            ref["reference_answer"] += " Уровень проверки: " + ref["grade"]
        for criterion in draft["criteria"]:
            for config in criterion["grade_configs"]:
                for anchor in config["anchors"]:
                    anchor["description"] += " Уровень: " + config["grade"]
        original = copy.deepcopy(draft)
        for index, grade in enumerate(GRADES):
            with self.subTest(grade=grade):
                interviewer, evaluator = runtime_payloads(draft, grade)
                criteria = [
                    {"criterion_id": c["id"], "description": c["description"]}
                    for c in draft["criteria"]
                ]
                self.assertEqual(interviewer, {"question": draft["text"], "criteria": criteria})
                self.assertEqual(
                    evaluator,
                    {
                        "question": draft["text"],
                        "target_level": grade.lower(),
                        "reference_answer": draft["grade_configs"][index]["reference_answer"],
                        "criteria": [
                            {**c, "anchors": source["grade_configs"][index]["anchors"]}
                            for c, source in zip(criteria, draft["criteria"], strict=False)
                        ],
                    },
                )
        self.assertEqual(draft, original)
        for grade in ("Lead", "middle", "", 2, None):
            with self.subTest(invalid_grade=grade), self.assertRaises(ValueError):
                runtime_payloads(draft, grade)
        middle = await self.generator(
            ScriptedProvider(completion(candidate()), completion(review()))
        ).generate(self.request())
        with self.assertRaises(ValueError):
            runtime_payloads(middle.questions[0], "Junior")
        for adapter in (knowledge_rows, lambda q: runtime_payloads(q, "Middle")):
            with self.assertRaises(ValidationError):
                adapter({**draft, "status": "published"})

    async def test_crash_after_durable_repair_response_replays_without_rebilling(self):
        # Crash exactly after the paid repair was saved, before slot state is updated.
        request = self.request()
        repaired = candidate(
            text="Как COUNT(email) в PostgreSQL учитывает NULL и отсутствие строк?"
        )
        provider = ScriptedProvider(
            completion(candidate()), completion(review("grade_fit")), completion(repaired)
        )
        save = Journal.save
        crashed = False

        def crash_after_repair(journal):
            nonlocal crashed
            save(journal)
            responses = [c for c in journal.data["calls"].values() if c.get("response")]
            if len(responses) == 3 and not crashed:
                crashed = True
                raise Crash()

        with patch.object(Journal, "save", crash_after_repair):
            with self.assertRaises(Crash):
                await self.generator(provider, max_rounds=2).generate(request)
        resumed = ScriptedProvider(completion(review()))
        result = await self.generator(resumed, max_rounds=2).generate(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.questions[0].text, repaired.text)
        self.assertEqual(len(resumed.calls), 1)
        self.assertEqual(result.metrics.calls, 4)
        self.assertAlmostEqual(result.metrics.cost_usd, 0.04)
        self.assertEqual(result.metrics.reserved_uncertain_usd, 0)


if __name__ == "__main__":
    unittest.main()
