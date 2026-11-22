import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import BaseModel

from app.errors import MLErrorCode, MLServiceError, ProviderNotConfigured
from app.generation import GenerationContext
from app.settings import Settings

_PROVIDER_OMITTED_KEYWORDS = {"title", "description", "oneOf", "allOf", "uniqueItems", "pattern"}
_FORMAT_EXAMPLES = {
    "InterviewerLLMResult": '{"action":"complete","message":null}',
    "EvaluatorLLMResult": (
        '{"criteria":[{"criterion_id":"<ID из рубрики>","score":0,"status":"absent",'
        '"comment":"<объяснение>","evidence":[]}],"summary":"<резюме>","recommendations":[]}'
    ),
}


def _provider_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project the application schema onto the keywords used in the provider request."""
    result = {key: value for key, value in schema.items() if key not in _PROVIDER_OMITTED_KEYWORDS}
    for key in ("properties", "$defs"):
        if key in result:
            result[key] = {name: _provider_schema(child) for name, child in result[key].items()}
    if "items" in result:
        result["items"] = _provider_schema(result["items"])
    if "anyOf" in result:
        result["anyOf"] = [_provider_schema(child) for child in result["anyOf"]]
    return result


def _output_format(model: type[BaseModel], provider: str) -> tuple[str, dict[str, Any]]:
    schema = model.model_json_schema()
    if provider == "deepinfra/fp8":
        example = _FORMAT_EXAMPLES[model.__name__]
        instruction = (
            f"Верни только один непустой JSON-объект. Обязательные поля и форма: {example}. "
            "Это пример формы, значения рассчитай для текущего диалога. "
            "Не возвращай {}, [], null или Markdown."
        )
        return instruction, {
            "type": "json_schema",
            "json_schema": {
                "name": model.__name__,
                "strict": True,
                "schema": _provider_schema(schema),
            },
        }
    # Alibaba rejects uniqueItems and may emit scalars/arrays in json_schema mode.
    # Full Pydantic and domain validation applies to both generation formats.
    instruction = (
        "Верни ровно один JSON-объект согласно следующей JSON Schema. "
        "Значение верхнего уровня не может быть null или массивом. "
        "Если поле допускает null, сохрани это поле внутри объекта.\nJSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False)
    )
    return instruction, {"type": "json_object"}


def retry_after_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        # Untrusted header sizes are bounded by the HTTP transport.
        try:
            return int(value)
        except ValueError:
            return None
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0, math.ceil((date - datetime.now(UTC)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


AUTH_CODES = {
    "invalid_api_key",
    "authentication_error",
    "permission_denied",
    "access_denied",
    "region_not_supported",
    "country_not_supported",
    "provider_access_denied",
}
QUOTA_CODES = {
    "insufficient_quota",
    "quota_exceeded",
    "insufficient_credits",
    "insufficient_balance",
    "billing_hard_limit_reached",
    "payment_required",
}
CONFIG_CODES = {
    "model_not_found",
    "invalid_model",
    "invalid_provider",
    "invalid_request_error",
    "unsupported_parameter",
    "unsupported_model",
    "no_endpoints_found",
}
CONFIG_PARAMS = {
    "model",
    "provider",
    "response_format",
    "temperature",
    "reasoning",
    "route",
    "endpoint",
}


def upstream_status(error: object) -> int | None:
    if not isinstance(error, dict):
        return None
    value = error.get("code")
    if isinstance(value, str) and value.isascii() and len(value) == 3 and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 400 <= value <= 599 else None


def upstream_error(status: int, error: object, retry_after: str | None) -> MLServiceError:
    # Only documented machine codes/parameter names are classified. Never search
    # arbitrary provider message text (which can contain echoed user input).
    signals = set()
    statuses = {status}
    parameter = None
    if isinstance(error, dict):
        for field in ("code", "type"):
            value = error.get(field)
            if isinstance(value, str):
                signals.add(value.lower())
        code = upstream_status(error)
        if code is not None:
            statuses.add(code)
        parameter = error.get("param")
    if signals & AUTH_CODES or statuses & {401, 403}:
        code = MLErrorCode.PROVIDER_AUTH_ERROR
    elif signals & QUOTA_CODES or 402 in statuses:
        code = MLErrorCode.PROVIDER_QUOTA_EXCEEDED
    elif signals & CONFIG_CODES or (
        statuses & {400, 404, 422} and isinstance(parameter, str) and parameter in CONFIG_PARAMS
    ):
        code = MLErrorCode.PROVIDER_CONFIG_ERROR
    elif statuses & {408, 504}:
        code = MLErrorCode.TIMEOUT
    elif 429 in statuses:
        return MLServiceError(
            MLErrorCode.RATE_LIMITED, retry_after_seconds=retry_after_seconds(retry_after)
        )
    elif any(code >= 500 for code in statuses):
        code = MLErrorCode.UNAVAILABLE
    else:
        code = MLErrorCode.INVALID_RESPONSE
    return MLServiceError(code)


class OpenRouterClient:
    """No transport retries. A request context counts physical calls and owns usage."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    @property
    def model_name(self) -> str:
        return self.settings.openrouter_model

    @property
    def provider_name(self) -> str:
        return self.settings.openrouter_provider

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.openrouter_api_key.get_secret_value().strip())

    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
        temperature: float,
        context: GenerationContext,
    ) -> str:
        key = self.settings.openrouter_api_key.get_secret_value().strip()
        if not key:
            raise ProviderNotConfigured()
        output_format, response_format = _output_format(
            response_model, self.settings.openrouter_provider
        )
        messages = [
            *messages[:2],
            {"role": "system", "content": output_format},
            *messages[2:],
        ]
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
            "response_format": response_format,
        }
        timeout = min(context.remaining(), self.settings.openrouter_timeout_seconds)
        metadata = context.usage.start_attempt()
        try:
            response = await self.http.post(
                f"{str(self.settings.openrouter_base_url).rstrip('/')}/chat/completions",
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
        if not response.is_success or (isinstance(body, dict) and "error" in body):
            metadata.upstream_status = upstream_status(error)
            raise upstream_error(response.status_code, error, response.headers.get("Retry-After"))

        if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
        if not body["choices"] or not isinstance(body["choices"][0], dict):
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
        choice = body["choices"][0]
        if "error" in choice:
            metadata.upstream_status = upstream_status(choice["error"])
            raise upstream_error(200, choice["error"], response.headers.get("Retry-After"))
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish in {
            "stop",
            "length",
            "content_filter",
            "error",
            "tool_calls",
        }:
            metadata.finish_reason = finish
        message = choice.get("message")
        if finish == "content_filter" or (
            isinstance(message, dict)
            and (
                message.get("refusal") is True
                or (isinstance(message.get("refusal"), str) and message["refusal"].strip())
            )
        ):
            raise MLServiceError(MLErrorCode.POLICY_REFUSAL)
        if finish == "length":
            raise MLServiceError(MLErrorCode.OUTPUT_TRUNCATED)
        if finish != "stop" or not isinstance(message, dict) or "content" not in message:
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
        if message.get("role", "assistant") != "assistant":
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
        content = message["content"]
        if content is None or (isinstance(content, str) and not content.strip()):
            raise MLServiceError(MLErrorCode.EMPTY_RESPONSE)
        if not isinstance(content, str):
            raise MLServiceError(MLErrorCode.INVALID_RESPONSE)
        return content
