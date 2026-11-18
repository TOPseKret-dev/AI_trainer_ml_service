"""Async OpenRouter adapter: no implicit retries, bounded response size, safe errors."""

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Protocol

import httpx


class ProviderFailure(Exception):
    def __init__(self, code, *, retryable=True, no_charge=False):
        super().__init__(code)
        self.code, self.retryable, self.no_charge = code, retryable, no_charge


@dataclass(frozen=True)
class Completion:
    content: str
    cost_usd: float | None
    model: str
    finish_reason: str
    generation_id: str = ""


class Provider(Protocol):
    async def complete(self, request: dict, timeout: float) -> Completion: ...


class OpenRouterProvider:
    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None):
        if not api_key or any(c.isspace() for c in api_key):
            raise ValueError("OpenRouter credential missing or invalid")
        self._key = api_key
        self._client = client

    async def complete(self, request, timeout):
        if self._client is not None:
            return await self._complete(self._client, request, timeout)
        async with httpx.AsyncClient() as client:
            return await self._complete(client, request, timeout)

    async def _complete(self, client, request, timeout):
        try:
            async with asyncio.timeout(timeout):
                async with client.stream(
                    "POST",
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": "Bearer " + self._key,
                        "X-Title": "InterSim Question Generation",
                    },
                    json=request,
                    timeout=httpx.Timeout(timeout, connect=min(10, timeout)),
                ) as response:
                    if response.status_code != 200:
                        code = response.status_code
                        raise ProviderFailure(
                            f"http_{code}",
                            retryable=code in {408, 429, 500, 502, 503, 504, 404},
                            no_charge=code in {400, 401, 402, 403, 404, 422, 429},
                        )
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 2_000_000:
                            raise ProviderFailure("response_too_large")
                        chunks.append(chunk)
            raw = b"".join(chunks).decode("utf-8").replace(self._key, "[REDACTED]")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ProviderFailure("provider_error")
            usage = data.get("usage")
            cost = usage.get("cost") if isinstance(usage, dict) else None
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                cost = None
            if data.get("error"):
                generation_id = data.get("id")
                return Completion(
                    "",
                    cost,
                    request["model"],
                    "error",
                    generation_id if isinstance(generation_id, str) else "",
                )
            choices = data.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else {}
            choice = choice if isinstance(choice, dict) else {}
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else ""
            if not isinstance(content, str):
                content = ""
            model = data.get("model")
            if not isinstance(model, str) or not model.strip() or len(model) > 128:
                model = request["model"]
            finish = choice.get("finish_reason")
            if not isinstance(finish, str):
                finish = "unknown"
            generation_id = data.get("id")
            if not isinstance(generation_id, str):
                generation_id = ""
            return Completion(content, cost, model, finish, generation_id)
        except ProviderFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderFailure("timeout_uncertain") from None
        except httpx.TransportError:
            raise ProviderFailure("transport_uncertain") from None
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
            raise ProviderFailure("invalid_provider_response") from None


def parse_content(raw):
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError("duplicate JSON property")
            output[key] = value
        return output

    content = raw.strip()
    # Only syntactic, meaning-preserving normalization. Never coerce enums or missing fields.
    if content.startswith("```json\n") and content.endswith("\n```"):
        content = content[8:-4]
    return json.loads(
        content,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
    )
