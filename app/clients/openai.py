"""Native OpenAI Responses API adapter with strict JSON Schema output.

The public HTTP contract remains provider-independent. Domain invariants are
still enforced by Pydantic and app.services.evaluator after generation.
"""
from typing import Any

import httpx
from pydantic import BaseModel

from app.clients.openrouter import upstream_error, upstream_status
from app.errors import MLErrorCode, MLServiceError, ProviderNotConfigured
from app.generation import GenerationContext
from app.settings import Settings

# JSON Schema composition constraints are not supported by OpenAI strict mode.
# They remain enforced by the application's full Pydantic/domain validation.
_UNSUPPORTED = {
    "allOf", "oneOf", "not", "if", "then", "else",
    "dependentRequired", "dependentSchemas", "uniqueItems", "default",
}


def strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project Pydantic's full schema onto the OpenAI strict-output subset.

    Only output format constraints are simplified. Never use this projection
    for request validation or for validating the completed model response.
    """
    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            key: visit(item)
            for key, item in value.items()
            if key not in _UNSUPPORTED
        }
        if result.get("type") == "object" or "properties" in result:
            result["additionalProperties"] = False
            result["required"] = list(result.get("properties", {}))
        return result

    projected = visit(schema)
    if projected.get("type") != "object":
        raise ValueError("Strict LLM result must be a JSON object at root")
    return projected


class OpenAIClient:
    """Provider adapter with no transport retries (semantic repair is upstream)."""

    provider_name = "openai"

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    @property
    def model_name(self) -> str:
        return self.settings.openai_model

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.openai_api_key.get_secret_value().strip())

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
        temperature: float,
        context: GenerationContext,
    ) -> str:
        # GPT reasoning models may reject temperature. It is intentionally not
        # forwarded; caller temperatures only affect the OpenRouter adapter.
        del temperature
        key = self.settings.openai_api_key.get_secret_value().strip()
        if not key:
            raise ProviderNotConfigured()
        timeout = min(context.remaining(), self.settings.openai_timeout_seconds)
        payload = {
            "model": self.model_name,
            "input": messages,
            "store": False,
            "max_output_tokens": self.settings.openai_max_output_tokens,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": strict_output_schema(response_model.model_json_schema()),
                },
            },
        }
        if self.settings.openai_reasoning_effort:
            payload["reasoning"] = {"effort": self.settings.openai_reasoning_effort}
        metadata = context.usage.start_attempt()
        try:
            response = await self.http.post(
                f"{str(self.settings.openai_base_url).rstrip('/')}/responses",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            raise MLServiceError(MLErrorCode.TIMEOUT) from None
        except (httpx.RemoteProtocolError, httpx.DecodingError):
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE) from None
        except httpx.UnsupportedProtocol:
            raise MLServiceError(MLErrorCode.PROVIDER_CONFIG_ERROR) from None
        except httpx.RequestError:
            raise MLServiceError(MLErrorCode.UNAVAILABLE) from None

        metadata.http_status = response.status_code
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            body = None
        metadata.record_usage(body)
        error = body.get("error") if isinstance(body, dict) else None
        if not response.is_success or (isinstance(body, dict) and error):
            metadata.upstream_status = upstream_status(error)
            raise upstream_error(response.status_code, error, response.headers.get("Retry-After"))
        if not isinstance(body, dict):
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)

        status = body.get("status")
        if status == "incomplete":
            metadata.finish_reason = "length"
            raise MLServiceError(MLErrorCode.OUTPUT_TRUNCATED)
        if status == "failed":
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
        if status != "completed":
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
        metadata.finish_reason = "stop"

        output = body.get("output")
        if not isinstance(output, list):
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue  # reasoning items, etc.
            for block in item.get("content", []):
                if not isinstance(block, dict):
                    raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
                if block.get("type") == "refusal":
                    raise MLServiceError(MLErrorCode.POLICY_REFUSAL)
                if block.get("type") == "output_text":
                    text = block.get("text")
                    if not isinstance(text, str):
                        raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
                    parts.append(text)
        content = "".join(parts)
        if not content.strip():
            raise MLServiceError(MLErrorCode.EMPTY_RESPONSE)
        return content
