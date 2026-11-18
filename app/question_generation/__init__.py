"""Autonomous generation API; the legacy offline CLI remains separate."""

from .contracts import GenerationRequest, GenerationResult
from .journal import IdempotencyConflict, JobBusy
from .provider import OpenRouterProvider
from .service import QuestionGenerator
from .settings import GenerationSettings, ModelPolicy

__all__ = [
    "GenerationRequest",
    "GenerationResult",
    "QuestionGenerator",
    "OpenRouterProvider",
    "GenerationSettings",
    "ModelPolicy",
    "IdempotencyConflict",
    "JobBusy",
]


async def generate_questions(request, *, api_key, state_dir, settings=None, cancel_event=None):
    """Convenience callable for an existing async ML service; no HTTP or DB coupling."""
    generator = QuestionGenerator(
        OpenRouterProvider(api_key), state_dir=state_dir, settings=settings
    )
    return await generator.generate(request, cancel_event=cancel_event)


__all__.append("generate_questions")
