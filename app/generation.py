"""One request, one monotonic deadline and an exact-or-unknown usage aggregate."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from app.errors import ProviderTimeout
from app.schemas.meta import MLResponseMeta, Operation


def utc_now() -> datetime:
    return datetime.now(UTC)


def token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass
class AttemptMetadata:
    http_status: int | None = None
    upstream_status: int | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None

    def record_usage(self, body: object) -> None:
        if not isinstance(body, dict) or not isinstance(body.get("usage"), dict):
            return
        usage = body["usage"]
        self.prompt_tokens = token_count(usage.get("prompt_tokens"))
        if self.prompt_tokens is None:
            self.prompt_tokens = token_count(usage.get("input_tokens"))
        self.completion_tokens = token_count(usage.get("completion_tokens"))
        if self.completion_tokens is None:
            self.completion_tokens = token_count(usage.get("output_tokens"))
        details = usage.get("completion_tokens_details")
        if details is None:
            details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            self.reasoning_tokens = token_count(details.get("reasoning_tokens"))


@dataclass
class UsageAccumulator:
    attempts: list[AttemptMetadata] = field(default_factory=list)

    def start_attempt(self) -> AttemptMetadata:
        if len(self.attempts) >= 2:
            raise RuntimeError("Provider attempt limit exceeded")
        attempt = AttemptMetadata()
        self.attempts.append(attempt)
        return attempt

    def total(self, name: str) -> int | None:
        if not self.attempts:
            return None if name == "reasoning_tokens" else 0
        values = [getattr(attempt, name) for attempt in self.attempts]
        if any(value is None for value in values):
            return None
        return sum(values)


@dataclass
class GenerationContext:
    operation: Operation
    started: float = field(default_factory=lambda: perf_counter())
    received_at: datetime = field(default_factory=lambda: utc_now())
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    usage: UsageAccumulator = field(default_factory=UsageAccumulator)
    deadline: float | None = None
    error_code: str | None = None

    def configure_deadline(self, local_limit: float, deadline_at: datetime | None) -> None:
        budget = min(55.0, local_limit)
        if deadline_at is not None:
            budget = min(budget, (deadline_at - self.received_at).total_seconds() - 5.0)
        self.deadline = self.started + budget
        self.remaining()

    def remaining(self) -> float:
        if self.deadline is None:
            raise RuntimeError("Generation deadline is not configured")
        remaining = self.deadline - perf_counter()
        if remaining <= 0:
            raise ProviderTimeout()
        return remaining

    def elapsed_ms(self) -> int:
        return max(0, int((perf_counter() - self.started) * 1000))

    def meta(self) -> MLResponseMeta:
        return MLResponseMeta(
            prompt_tokens=self.usage.total("prompt_tokens"),
            completion_tokens=self.usage.total("completion_tokens"),
            reasoning_tokens=self.usage.total("reasoning_tokens"),
            duration_ms=self.elapsed_ms(),
            provider_attempts=len(self.usage.attempts),
        )
