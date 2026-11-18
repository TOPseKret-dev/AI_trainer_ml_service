"""HTTP service boundary; the vendored generation engine remains independently testable."""

import fcntl
import json
import logging
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.question_generation import (
    GenerationSettings,
    IdempotencyConflict,
    JobBusy,
    OpenRouterProvider,
    QuestionGenerator,
)
from app.question_generation_http import QuestionGenerationError
from app.schemas.question_generation import QuestionGenerationErrorCode as Code
from app.schemas.question_generation import QuestionGenerationRequest, QuestionGenerationResponse

logger = logging.getLogger(__name__)


class QuestionGenerationConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUESTIONGEN_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )
    state_dir: Path = Path("artifacts/question-generation")
    max_concurrent: int = Field(default=1, ge=1, le=8)
    api_key: SecretStr = SecretStr("")
    policy: GenerationSettings = GenerationSettings()


@contextmanager
def exclusive(path: Path, code: Code):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise QuestionGenerationError(code) from None
        yield


class QuestionGenerationService:
    def __init__(self, provider, config: QuestionGenerationConfig):
        self.config = config
        self.generator = QuestionGenerator(
            provider, state_dir=config.state_dir, settings=config.policy
        )

    async def generate(self, request: QuestionGenerationRequest) -> QuestionGenerationResponse:
        if self.generator.provider is None:
            raise QuestionGenerationError(Code.PROVIDER_NOT_CONFIGURED)
        try:
            root = self.config.state_dir
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = root.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                # Never chmod a misconfigured shared directory such as /tmp.
                raise QuestionGenerationError(Code.STORAGE_UNAVAILABLE)
            # Both admission locks and library journals are private local files.
            # A shared Docker volume coordinates workers on one Linux host.
            with exclusive(root / f".http-{request.request_id}.lock", Code.JOB_BUSY):
                for slot in range(self.config.max_concurrent):
                    try:
                        with exclusive(root / f".capacity-{slot}.lock", Code.CAPACITY_EXCEEDED):
                            result = await self.generator.generate(request)
                            logger.info(
                                "event=question_generation_result request_id=%s status=%s "
                                "questions=%d calls=%d cost_usd=%s reserved_uncertain_usd=%s",
                                request.request_id,
                                result.status,
                                len(result.questions),
                                result.metrics.calls,
                                result.metrics.cost_usd,
                                result.metrics.reserved_uncertain_usd,
                            )
                            return QuestionGenerationResponse.model_validate(result.model_dump())
                    except QuestionGenerationError as exc:
                        if exc.code != Code.CAPACITY_EXCEEDED:
                            raise
                raise QuestionGenerationError(Code.CAPACITY_EXCEEDED)
        except IdempotencyConflict:
            raise QuestionGenerationError(Code.IDEMPOTENCY_CONFLICT) from None
        except JobBusy:
            raise QuestionGenerationError(Code.JOB_BUSY) from None
        except (OSError, json.JSONDecodeError):
            raise QuestionGenerationError(Code.STORAGE_UNAVAILABLE) from None


def create_question_generation_service(http, settings):
    config = QuestionGenerationConfig()
    key = (
        config.api_key.get_secret_value().strip()
        or settings.openrouter_api_key.get_secret_value().strip()
    )
    provider = (
        OpenRouterProvider(key, client=http)
        if key and not any(char.isspace() for char in key)
        else None
    )
    return QuestionGenerationService(provider, config)
