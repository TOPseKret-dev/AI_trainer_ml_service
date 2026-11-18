"""One awaited operation: generate, validate, review, repair, return only draft content."""

import asyncio
import json
import re
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from .contracts import (
    Candidate,
    DraftQuestion,
    GenerationPlan,
    GenerationRequest,
    GenerationResult,
    Review,
)
from .journal import Journal, fingerprint
from .prompts import VERSION, messages
from .provider import Completion, ProviderFailure, parse_content
from .rubric import compile_rubric
from .settings import GenerationSettings


class StopGeneration(Exception):
    def __init__(self, code):
        self.code = code


class InvalidGeneration(ValueError):
    pass


def normalize(text):
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def local_check(candidate, request, existing):
    if [g.grade for g in candidate.grade_configs] != request.grades:
        raise InvalidGeneration("grade_configs must cover requested minimum grade through Senior")
    if not re.search(r"[а-яА-Я]{3}", candidate.text):
        raise InvalidGeneration("question must be in Russian")
    for text in [candidate.text, *[g.reference_answer for g in candidate.grade_configs]]:
        if re.search(r"\[STUB|\bTODO\b|\bLorem ipsum\b", text, re.I):
            raise InvalidGeneration("placeholder content forbidden")
    current = normalize(candidate.text)
    for old in existing:
        if current == normalize(old["text"]):
            raise InvalidGeneration("exact duplicate of existing question")


def diagnostics(error):
    if isinstance(error, ValidationError):
        return [
            {"path": ".".join(map(str, e["loc"])), "type": e["type"]}
            for e in error.errors(include_input=False, include_context=False)
        ][:20]
    if isinstance(error, InvalidGeneration):
        return [{"path": "candidate", "type": str(error)}]
    return [{"path": "response", "type": "invalid_or_truncated_json"}]


def context_materials(request):
    """Ship the extracted matrix labels; no network retrieval or invented citations."""
    from importlib.resources import files

    catalog = json.loads(files(__package__).joinpath("catalog.json").read_text())
    aliases = {
        "sql": "sql",
        "etl": "etl",
        "dwh": "dwh",
        "кхд": "dwh",
        "требования": "requirements",
        "сбор требований": "requirements",
        "интеграции": "integration",
        "базы данных": "databases",
        "транзакции": "databases",
        "качество данных": "etl",
    }
    area = aliases.get(normalize(request.area), normalize(request.area))
    entries = [c for c in catalog["competencies"] if c["area"] == area]
    built_in = [
        {
            "id": "matrix:" + c["competency_id"],
            "text": "; ".join(r["label"] for r in c["source_refs"]) + ". " + c["interpretation"],
        }
        for c in entries
    ]
    return built_in + [m.model_dump() for m in request.materials]


def totals(journal):
    known = sum(Decimal(str(c.get("cost", 0))) for c in journal.data["calls"].values())
    uncertain = sum(
        Decimal(str(c["reservation"]))
        for c in journal.data["calls"].values()
        if c["status"] in {"pending", "uncertain"}
    )
    return known, uncertain


class QuestionGenerator:
    """Reuse the instance; jobs are isolated. Backend owns authentication and job scheduling.

    state_dir must be a private durable local directory shared by all workers on one host.
    Use the same request_id for retries; a completed result is returned without paid calls.
    """

    def __init__(
        self, provider, *, state_dir: str | Path, settings: GenerationSettings | None = None
    ):
        self.provider = provider
        self.state_dir = state_dir
        self.settings = settings or GenerationSettings()

    async def generate(
        self, request: GenerationRequest | dict, *, cancel_event: asyncio.Event | None = None
    ) -> GenerationResult:
        request = GenerationRequest.model_validate(request)
        job_id = request.request_id or str(uuid4())
        identity = fingerprint(
            {
                "request": request.model_dump(exclude={"request_id"}),
                "settings": self.settings.model_dump(),
                "prompt": VERSION,
                "materials": context_materials(request),
            }
        )
        with Journal(self.state_dir, job_id, identity) as journal:
            if journal.data["result"] is not None:
                return GenerationResult.model_validate(journal.data["result"])
            runner = _Run(self, request, job_id, journal, cancel_event)
            try:
                result = await runner.run()
            except asyncio.CancelledError:
                # Do not swallow task cancellation. Preserve all paid responses and candidates.
                journal.save()
                raise
            journal.data["result"] = result.model_dump()
            journal.save()
            return result


class _Run:
    def __init__(self, generator, request, job_id, journal, cancel):
        self.generator, self.request, self.job_id = generator, request, job_id
        self.settings, self.journal, self.cancel = generator.settings, journal, cancel
        self.start = time.monotonic()
        # A resumed crash does not reset the overall time allowance.
        self.started_at = journal.data.setdefault("started_at", time.time())
        journal.save()
        self.end = self.start + max(
            0, self.settings.deadline_seconds - (time.time() - self.started_at)
        )
        self.warnings = []
        self.materials = context_materials(request)
        self.plan = None
        self.existing = [q.model_dump() for q in request.existing_questions]

    def checkpoint(self):
        if self.cancel and self.cancel.is_set():
            raise StopGeneration("cancelled")
        if time.monotonic() >= self.end:
            raise StopGeneration("deadline_exceeded")
        known, uncertain = totals(self.journal)
        if known + uncertain >= Decimal(str(self.settings.max_cost_usd)):
            raise StopGeneration("budget_exhausted")

    async def transport(self, body, timeout):
        task = asyncio.create_task(self.generator.provider.complete(body, timeout))
        cancelled = asyncio.create_task(self.cancel.wait()) if self.cancel else None
        try:
            pending = {task} | ({cancelled} if cancelled else set())
            done, _ = await asyncio.wait(
                pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            if cancelled and cancelled in done:
                raise StopGeneration("cancelled")
            raise ProviderFailure("timeout_uncertain")
        finally:
            for future in [task, cancelled]:
                if future is not None and not future.done():
                    future.cancel()
            await asyncio.gather(*[f for f in [task, cancelled] if f], return_exceptions=True)

    async def call(self, stage, data, schema, policy, call_key):
        body = {
            "model": policy.model,
            "messages": messages(stage, data),
            "max_tokens": (
                4000
                if stage == "plan"
                else self.settings.reviewer_tokens
                if stage in {"review", "distinct"}
                else self.settings.writer_tokens
            ),
            "reasoning": {"effort": "low"},
            "provider": {
                "require_parameters": True,
                "allow_fallbacks": True,
                "max_price": {
                    "prompt": policy.input_usd_per_million,
                    "completion": policy.output_usd_per_million,
                },
            },
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
        }
        size = len(json.dumps(body, ensure_ascii=False).encode())
        if size > self.settings.max_request_bytes:
            raise StopGeneration("context_limit")
        identity = fingerprint(body)
        previous = self.journal.data["calls"].get(call_key)
        if previous:
            if previous["identity"] != identity:
                raise RuntimeError("checkpoint call identity mismatch")
            if previous.get("response"):
                return Completion(**previous["response"])
            # A crashed/pending request may have been billed. Reserve its full ceiling;
            # move on to the next bounded attempt, never call this attempt again.
            raise ProviderFailure(
                previous.get("error", "interrupted_uncertain"),
                retryable=previous.get("retryable", True),
            )
        self.checkpoint()
        reservation = (
            Decimal(size + 1024) * Decimal(str(policy.input_usd_per_million))
            + Decimal(body["max_tokens"]) * Decimal(str(policy.output_usd_per_million))
        ) / Decimal(1_000_000)
        known, uncertain = totals(self.journal)
        if known + uncertain + reservation > Decimal(str(self.settings.max_cost_usd)):
            raise StopGeneration("budget_exhausted")
        entry = {
            "identity": identity,
            "reservation": float(reservation),
            "cost": 0,
            "status": "pending",
            "model": policy.model,
            "stage": stage,
        }
        self.journal.data["calls"][call_key] = entry
        self.journal.save()  # durable intent BEFORE the network operation
        # Disk IO can consume the remaining deadline; never dispatch a known-expired call.
        cancelled = self.cancel and self.cancel.is_set()
        if cancelled or time.monotonic() >= self.end:
            entry.update(status="failed", error="not_dispatched", dispatched=False)
            self.journal.save()
            raise StopGeneration("cancelled" if cancelled else "deadline_exceeded")
        try:
            result = await self.transport(
                body,
                min(self.settings.call_timeout_seconds, max(0.001, self.end - time.monotonic())),
            )
        except ProviderFailure as error:
            entry.update(
                status="failed" if error.no_charge else "uncertain",
                error=error.code,
                retryable=error.retryable,
            )
            self.journal.save()
            raise
        except (StopGeneration, asyncio.CancelledError):
            entry.update(status="uncertain", error="cancelled_uncertain")
            self.journal.save()
            raise
        entry.update(
            response=asdict(result),
            cost=result.cost_usd or 0,
            status="completed" if result.cost_usd is not None else "uncertain",
        )
        self.journal.save()
        return result

    async def structured(self, stage, data, schema, slot, round_no):
        feedback = None
        for attempt in range(self.settings.attempts_per_stage):
            policy = (
                self.settings.reviewer
                if stage in {"review", "distinct"}
                else (self.settings.writer if attempt == 0 else self.settings.fallback_writer)
            )
            payload = {**data, **({"format_errors": feedback} if feedback else {})}
            try:
                response = await self.call(
                    stage, payload, schema, policy, f"{slot}:{round_no}:{stage}:{attempt}"
                )
                if response.finish_reason != "stop":
                    raise InvalidGeneration("incomplete output: " + response.finish_reason)
                value = schema.model_validate(parse_content(response.content))
                if stage == "distinct" and any(
                    not passed
                    for key, passed in value.checks.model_dump().items()
                    if key != "distinctness"
                ):
                    raise InvalidGeneration("distinct review may fail only distinctness check")
                if stage == "plan":
                    intents = [normalize(t.intent) for t in value.targets]
                    if len(intents) != self.request.count or len(set(intents)) != len(intents):
                        raise InvalidGeneration("plan must contain exactly count distinct targets")
                if stage == "write":
                    value = compile_rubric(value)
                    local_check(value, self.request, self.existing)
                return value, response.model
            except ProviderFailure as error:
                if not error.retryable:
                    raise StopGeneration(error.code) from None
                feedback = [{"type": error.code}]
                if attempt + 1 < self.settings.attempts_per_stage:
                    await asyncio.sleep(
                        min(0.5 * (attempt + 1), max(0, self.end - time.monotonic()))
                    )
            except (ValidationError, InvalidGeneration, ValueError, TypeError) as error:
                feedback = diagnostics(error)
        raise InvalidGeneration(json.dumps(feedback, ensure_ascii=False))

    async def run(self):
        if not self.request.bank_context_complete:
            self.warn(
                "bank_context_incomplete",
                "Уникальность проверена только относительно переданных вопросов.",
            )
        if not self.materials:
            self.warn(
                "no_source_materials", "Материалы области не переданы; использованы знания модели."
            )
        questions, rejected = [], []
        stopped = None
        try:
            if self.settings.plan_slots:
                try:
                    self.plan, _ = await self.structured(
                        "plan",
                        {
                            "area": self.request.area,
                            "comment": self.request.comment,
                            "min_grade": self.request.grade,
                            "count": self.request.count,
                            "materials": self.materials,
                            "existing_questions": self.existing,
                        },
                        GenerationPlan,
                        -1,
                        0,
                    )
                    self.journal.data["plan"] = self.plan.model_dump()
                    self.journal.save()
                except InvalidGeneration:
                    raise StopGeneration("planning_failed") from None
            for slot in range(self.request.count):
                state = self.journal.data["slots"].setdefault(str(slot), {})
                if state.get("accepted"):
                    question = DraftQuestion.model_validate(state["accepted"])
                elif state.get("finished"):
                    if state.get("rejected"):
                        rejected.append(state["rejected"])
                    continue
                else:
                    question = await self.slot(slot, state)
                if question:
                    questions.append(question.model_dump())
                    self.existing.append(
                        {
                            "id": question.id,
                            "text": question.text,
                            "skills": question.skills,
                            "area": question.area,
                        }
                    )
                elif state.get("rejected"):
                    rejected.append(state["rejected"])
        except StopGeneration as error:
            stopped = error.code
            self.warn(
                error.code,
                "Генерация остановлена; готовые вопросы и доступные кандидаты сохранены.",
            )
        # Preserve an in-flight, structurally valid candidate even if review could not run.
        for slot, state in self.journal.data["slots"].items():
            if (
                state.get("candidate")
                and not state.get("accepted")
                and not any(r["slot"] == int(slot) for r in rejected)
            ):
                rejected.append(self.rejected(int(slot), state))
        if len(questions) < self.request.count:
            self.warn(
                "count_shortfall",
                f"Получено {len(questions)} из {self.request.count} проверенных черновиков.",
            )
        known, uncertain = totals(self.journal)
        if known + uncertain > Decimal(str(self.settings.max_cost_usd)):
            self.warn(
                "cost_overrun",
                "Провайдер сообщил стоимость выше расчётного лимита; новые вызовы остановлены.",
            )
        if uncertain:
            self.warn(
                "cost_uncertain", "Часть расходов неизвестна; для лимита удержана верхняя оценка."
            )
        status = (
            "cancelled"
            if stopped == "cancelled"
            else "completed"
            if len(questions) == self.request.count
            else "partial"
            if questions
            else "failed"
        )
        return GenerationResult.model_validate(
            {
                "job_id": self.job_id,
                "status": status,
                "questions": questions,
                "rejected_candidates": rejected,
                "warnings": self.warnings,
                "metrics": {
                    "calls": sum(
                        c.get("dispatched", True) for c in self.journal.data["calls"].values()
                    ),
                    "cost_usd": float(known),
                    "reserved_uncertain_usd": float(uncertain),
                    "elapsed_seconds": round(time.time() - self.started_at, 3),
                },
            }
        )

    def warn(self, code, message, slot=None):
        self.warnings.append({"code": code, "message": message, "slot": slot})

    def rejected(self, slot, state):
        return {
            "slot": slot,
            "candidate": state["candidate"],
            "quality_status": "needs_revision" if state.get("review") else "unverified",
            "issues": (state.get("review") or {}).get("defects", [])
            or [
                {
                    "severity": "major",
                    "path": "review",
                    "explanation": "Проверка качества не завершена.",
                    "required_change": "Нужна независимая проверка перед использованием.",
                }
            ],
        }

    async def slot(self, slot, state):
        data = {
            "area": self.request.area,
            "comment": self.request.comment,
            "min_grade": self.request.grade,
            "grades": self.request.grades,
            "slot": slot + 1,
            "count": self.request.count,
            "materials": self.materials,
            "existing_questions": self.existing,
        }
        if self.plan:
            data.update(
                target=self.plan.targets[slot].model_dump(),
                all_targets=[t.model_dump() for t in self.plan.targets],
            )
        base_data = dict(data)
        for round_no in range(self.settings.max_rounds):
            # Replay paid responses after crash from the beginning; identities remain stable.
            if round_no:
                data = {
                    **data,
                    "previous_candidate": state.get("candidate"),
                    "feedback": state.get("review") or state.get("errors"),
                    "repair_mode": "redesign"
                    if round_no >= 2
                    and any(
                        not passed
                        for check, passed in (state.get("review") or {}).get("checks", {}).items()
                        if check
                        in {"distinctness", "scope_alignment", "grade_fit", "factual_correctness"}
                    )
                    else "repair",
                }
            data = state.setdefault("round_inputs", {}).setdefault(str(round_no), data)
            self.journal.save()
            try:
                candidate, writer = await self.structured("write", data, Candidate, slot, round_no)
                state.update(
                    candidate=candidate.model_dump(),
                    writer=writer,
                    review=None,
                    distinct_review=None,
                )
                self.journal.save()
                review, reviewer = await self.structured(
                    "review",
                    {**base_data, "candidate": candidate.model_dump()},
                    Review,
                    slot,
                    round_no,
                )
                if (
                    self.settings.semantic_deduplication
                    and self.existing
                    and not any(d.severity in {"major", "critical"} for d in review.defects)
                ):
                    distinct, _ = await self.structured(
                        "distinct",
                        {
                            "area": self.request.area,
                            "comment": self.request.comment,
                            "candidate": {
                                "text": candidate.text,
                                "skills": candidate.skills,
                                "criteria": [c.description for c in candidate.criteria],
                            },
                            "existing_questions": self.existing,
                        },
                        Review,
                        slot,
                        round_no,
                    )
                    state["distinct_review"] = distinct.model_dump()
                    if any(d.severity in {"major", "critical"} for d in distinct.defects):
                        review = distinct
                state["review"] = review.model_dump()
                self.journal.save()
                if not any(d.severity in {"major", "critical"} for d in review.defects):
                    question = DraftQuestion.model_validate(
                        {
                            **candidate.model_dump(),
                            "id": str(uuid5(UUID(self.job_id), str(slot))),
                            "specialization": "analytics",
                            "area": self.request.area,
                            "min_grade": self.request.grade,
                            "grades": self.request.grades,
                            "status": "draft",
                            "generation": {
                                "source": "model_generated",
                                "comment": self.request.comment,
                                "model": writer,
                                "reviewer_model": reviewer,
                                "prompt_version": VERSION,
                                "plan_target": self.plan.targets[slot].intent
                                if self.plan
                                else None,
                                "material_ids": [m["id"] for m in self.materials],
                                "quality_status": "passed",
                                "review": review.model_dump(),
                                "distinctness_review": state.get("distinct_review"),
                            },
                        }
                    )
                    state.update(accepted=question.model_dump(), finished=True)
                    self.journal.save()
                    return question
            except InvalidGeneration as error:
                state["errors"] = str(error)
                self.journal.save()
        state["finished"] = True
        if state.get("candidate"):
            state["rejected"] = self.rejected(slot, state)
        self.warn(
            "quality_attempts_exhausted",
            "Исчерпан лимит исправлений вопроса."
            + (
                " Последняя техническая ошибка: " + state["errors"][:700]
                if state.get("errors")
                else ""
            ),
            slot,
        )
        self.journal.save()
        return None
