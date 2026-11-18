"""Bound generation input, isolate safe errors and cancel work on disconnect."""

import asyncio
import logging
from time import perf_counter
from uuid import uuid4

from starlette.responses import JSONResponse

from app.schemas.question_generation import (
    QuestionGenerationErrorCode as Code,
)
from app.schemas.question_generation import QuestionGenerationErrorResponse

PATH = "/api/v1/question-generation"
WIRE_LIMIT = 262144
logger = logging.getLogger("ml_service.question_generation")

# HTTP failures are separate from terminal generation results (including partial).
ERRORS = {
    Code.INVALID_INPUT: (422, False, "Invalid question generation input"),
    Code.PAYLOAD_TOO_LARGE: (413, False, "Question generation body is too large"),
    Code.UNSUPPORTED_MEDIA_TYPE: (415, False, "Expected application/json"),
    Code.REQUEST_TIMEOUT: (408, True, "Question generation body upload timed out"),
    Code.IDEMPOTENCY_CONFLICT: (409, False, "Request ID already uses different input or policy"),
    Code.JOB_BUSY: (409, True, "This question generation request is already running"),
    Code.CAPACITY_EXCEEDED: (503, True, "Question generation capacity is occupied"),
    Code.PROVIDER_NOT_CONFIGURED: (503, False, "Question generation provider is not configured"),
    Code.STORAGE_UNAVAILABLE: (503, True, "Question generation state storage is unavailable"),
    Code.INTERNAL_ERROR: (500, False, "Internal question generation error"),
}


class QuestionGenerationError(Exception):
    def __init__(self, code: Code):
        self.code = code
        super().__init__(code.value)


def generation_error_response(scope, code: Code):
    status, retryable, message = ERRORS[code]
    state = scope.setdefault("state", {})
    correlation_id = state.setdefault("qgen_correlation_id", uuid4().hex)
    retry_after = 2 if code in {Code.JOB_BUSY, Code.CAPACITY_EXCEEDED} else None
    result = QuestionGenerationErrorResponse.model_validate(
        {
            "error": {
                "code": code,
                "message": message,
                "details": {
                    "request_id": state.get("qgen_request_id"),
                    "correlation_id": correlation_id,
                    "retryable": retryable,
                    "retry_after_seconds": retry_after,
                },
            }
        }
    )
    headers = {"X-Correlation-ID": correlation_id}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        result.model_dump(mode="json", exclude_none=True), status_code=status, headers=headers
    )


class QuestionGenerationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != PATH or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        scope.setdefault("state", {})["qgen_correlation_id"] = uuid4().hex
        started = perf_counter()
        status = 500
        response_started = False
        response_finished = False

        async def capture(message):
            nonlocal status, response_started, response_finished
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"x-correlation-id"
                ]
                headers.append(
                    (b"x-correlation-id", scope["state"]["qgen_correlation_id"].encode())
                )
                message = {**message, "headers": headers}
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True
            await send(message)

        async def reject(code):
            await generation_error_response(scope, code)(scope, receive, capture)

        task = watcher = None
        cancelled = False
        try:
            headers = dict(scope.get("headers", []))
            content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            if content_type != b"application/json":
                return await reject(Code.UNSUPPORTED_MEDIA_TYPE)
            length = headers.get(b"content-length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError:
                    return await reject(Code.INVALID_INPUT)
                if size < 0:
                    return await reject(Code.INVALID_INPUT)
                if size > WIRE_LIMIT:
                    return await reject(Code.PAYLOAD_TOO_LARGE)
            body = bytearray()
            guard = asyncio.timeout(30)
            try:
                async with guard:
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            cancelled = True
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > WIRE_LIMIT:
                            return await reject(Code.PAYLOAD_TOO_LARGE)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                if guard.expired():
                    return await reject(Code.REQUEST_TIMEOUT)
                raise
            delivered = asyncio.Event()

            async def replay():
                if not delivered.is_set():
                    delivered.set()
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            async def watch_disconnect():
                await delivered.wait()
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        if not response_finished:
                            task.cancel()
                        return

            task = asyncio.create_task(self.app(scope, replay, capture))
            watcher = asyncio.create_task(watch_disconnect())
            await task
        except asyncio.CancelledError:
            cancelled = True
            raise
        except QuestionGenerationError as exc:
            if response_started:
                raise
            await reject(exc.code)
        except Exception as exc:
            if response_started:
                raise
            # Values and traceback can contain provider/candidate text or credentials.
            logger.error("event=generation_exception exception_type=%s", type(exc).__name__)
            await reject(Code.INTERNAL_ERROR)
        finally:
            for future in (watcher, task):
                if future is not None and not future.done():
                    future.cancel()
            await asyncio.gather(
                *[f for f in (watcher, task) if f is not None], return_exceptions=True
            )
            logger.info(
                "event=question_generation_http correlation_id=%s request_id=%s "
                "status=%d cancelled=%s duration_ms=%d",
                scope["state"]["qgen_correlation_id"],
                scope["state"].get("qgen_request_id"),
                status,
                cancelled,
                max(0, round((perf_counter() - started) * 1000)),
            )
