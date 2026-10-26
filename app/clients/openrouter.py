from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.errors import (
    InvalidProviderResponse,
    ProviderNotConfigured,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.settings import Settings


@dataclass
class AttemptMetadata:
    http_status: int | None = None
    upstream_status: int | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None


class TokenDetails(BaseModel):
    reasoning_tokens: int | None = Field(default=None, ge=0, strict=True)


class Usage(BaseModel):
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens_details: TokenDetails | None = None


def raise_upstream_error(status: int) -> None:
    if status in {408, 504}:
        raise ProviderTimeout()
    if status in {401, 402, 403, 404, 429} or status >= 500:
        raise ProviderUnavailable()
    raise InvalidProviderResponse()


class OpenRouterClient:
    """Transport only; the caller owns AsyncClient lifetime and result validation."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
        temperature: float,
        metadata: AttemptMetadata,
    ) -> str:
        key = self.settings.openrouter_api_key.get_secret_value().strip()
        if not key:
            raise ProviderNotConfigured()
        payload = {
            "model": self.settings.openrouter_model,
            "messages": messages,
            "temperature": temperature,
            "provider": {
                "order": [self.settings.openrouter_provider],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
            "reasoning": {"enabled": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            },
        }
        try:
            response = await self.http.post(
                f"{str(self.settings.openrouter_base_url).rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=self.settings.openrouter_timeout_seconds,
            )
        except httpx.TimeoutException:
            raise ProviderTimeout() from None
        except httpx.RequestError:
            raise ProviderUnavailable() from None
        metadata.http_status = response.status_code
        if not response.is_success:
            raise_upstream_error(response.status_code)
        try:
            body: Any = response.json()
            if not isinstance(body, dict):
                raise ValueError
            if body.get("usage") is not None:
                usage = Usage.model_validate(body["usage"])
                metadata.input_tokens = usage.prompt_tokens
                metadata.output_tokens = usage.completion_tokens
                if usage.completion_tokens_details:
                    metadata.reasoning_tokens = usage.completion_tokens_details.reasoning_tokens
            # OpenRouter may report a provider error inside a successful HTTP envelope.
            if "error" in body:
                error = body["error"]
                code = error.get("code") if isinstance(error, dict) else None
                if type(code) is int and 400 <= code <= 599:
                    metadata.upstream_status = code
                    raise_upstream_error(code)
                raise ValueError
            choice = body["choices"][0]
            finish_reason = choice.get("finish_reason")
            if finish_reason in {"stop", "length", "error", "content_filter", "tool_calls"}:
                metadata.finish_reason = finish_reason
            if finish_reason != "stop":
                raise ValueError
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            return content
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, ValidationError):
            raise InvalidProviderResponse() from None
