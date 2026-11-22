from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_openrouter_client
from app.clients.openai import OpenAIClient
from app.clients.openrouter import OpenRouterClient
from app.errors import ProviderNotConfigured
from app.schemas.common import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
@router.get("/health/live", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="ml-service")


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(
    client: Annotated[OpenRouterClient | OpenAIClient, Depends(get_openrouter_client)],
) -> ReadinessResponse:
    """Check local LLM configuration without a provider request or inference charge."""
    if not client.is_configured:
        raise ProviderNotConfigured()
    return ReadinessResponse(status="ready", service="ml-service")
