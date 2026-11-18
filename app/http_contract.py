"""Endpoint-scoped v2 errors, request timing and disconnect cancellation."""

import asyncio
import json
import logging
from contextlib import suppress
from time import perf_counter

from fastapi import Request
from starlette.responses import JSONResponse

from app.errors import MLErrorCode, MLServiceError
from app.generation import GenerationContext
from app.schemas.meta import EvaluatorErrorResponse, InterviewerErrorResponse

logger = logging.getLogger("ml_service.requests")
OPERATIONS = {"/api/v1/interviewer": "interviewer", "/api/v1/evaluator": "evaluator"}


def safe_meta(context: GenerationContext):
    try:
        return context.meta()
    except Exception:
        logger.error("event=metadata_unavailable correlation_id=%s", context.correlation_id)
        return None


def error_response(context: GenerationContext, exc: MLServiceError) -> JSONResponse:
    context.error_code = exc.code
    model = (
        InterviewerErrorResponse if context.operation == "interviewer" else EvaluatorErrorResponse
    )
    data = {
        "error": {
            "code": exc.code,
            "message": exc.detail,
            "details": {
                "operation": context.operation,
                "retryable": exc.retryable,
                "correlation_id": context.correlation_id,
                "provider_attempts": len(context.usage.attempts),
            },
        }
    }
    headers = {"X-Correlation-ID": context.correlation_id}
    if exc.retry_after_seconds is not None:
        data["error"]["details"]["retry_after_seconds"] = exc.retry_after_seconds
        headers["Retry-After"] = str(exc.retry_after_seconds)
    meta = safe_meta(context)
    if meta is not None:
        data["meta"] = meta
    result = model.model_validate(data)
    return JSONResponse(
        result.model_dump(mode="json"), status_code=exc.status_code, headers=headers
    )


def get_context(request: Request) -> GenerationContext | None:
    return getattr(request.state, "generation_context", None)


class MLContractMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        operation = OPERATIONS.get(scope["path"])
        if operation is None:
            return await self.legacy(scope, receive, send)
        context = GenerationContext(operation)
        scope.setdefault("state", {})["generation_context"] = context
        body_received = asyncio.Event()
        status = 500
        start_message = None
        chunks = []
        response_started = False
        response_finished = False

        async def app_receive():
            message = await receive()
            if message["type"] == "http.disconnect":
                raise asyncio.CancelledError
            if message["type"] == "http.request" and not message.get("more_body", False):
                body_received.set()
            return message

        async def response_send(message):
            nonlocal status, start_message, response_started, response_finished
            if message["type"] == "http.response.start":
                start_message = message
                status = message["status"]
                return
            if message["type"] != "http.response.body":
                return await send(message)
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            payload = json.loads(b"".join(chunks))
            if status == 200 and context.deadline is not None:
                try:
                    context.remaining()
                except MLServiceError as exc:
                    # Starlette has seen its response.start, but we have not sent
                    # headers yet. Replace the buffered response here so a late
                    # deadline is not wrapped as an internal response-start error.
                    chunks.clear()
                    await error_response(context, exc)(scope, app_receive, response_send)
                    return
            meta = safe_meta(context)
            if meta is not None:
                payload["meta"] = meta.model_dump(mode="json")
            elif "error" in payload:
                payload.pop("meta", None)
            else:
                raise RuntimeError("Success metadata unavailable")
            body = json.dumps(
                payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            headers = [
                (k, v)
                for k, v in start_message.get("headers", [])
                if k.lower() not in {b"content-length", b"content-type", b"x-correlation-id"}
            ]
            headers.extend(
                [
                    (b"content-length", str(len(body)).encode()),
                    (b"content-type", b"application/json"),
                    (b"x-correlation-id", context.correlation_id.encode()),
                ]
            )
            response_started = True
            await send({**start_message, "headers": headers})
            response_finished = True
            await send({"type": "http.response.body", "body": body})

        async def run():
            nonlocal start_message
            try:
                await self.app(scope, app_receive, response_send)
            except asyncio.CancelledError:
                raise
            except MLServiceError as exc:
                if response_started:
                    raise
                start_message = None
                chunks.clear()
                await error_response(context, exc)(scope, app_receive, response_send)
            except Exception:
                if response_started:
                    raise
                # No exception repr, traceback, provider payload or validation input.
                start_message = None
                chunks.clear()
                response = error_response(context, MLServiceError(MLErrorCode.INTERNAL_ERROR))
                await response(scope, app_receive, response_send)

        async def watch_disconnect(task):
            await body_received.wait()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    if not response_finished:
                        task.cancel()
                    return

        task = asyncio.create_task(run())
        watcher = asyncio.create_task(watch_disconnect(task))
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            meta = safe_meta(context)
            logger.log(
                logging.INFO if status < 400 and not cancelled else logging.WARNING,
                "endpoint=%s method=%s outcome=%s status=%d duration_ms=%.2f "
                "operation=%s correlation_id=%s provider_attempts=%d error_code=%s "
                "prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s",
                getattr(scope.get("route"), "name", "<unmatched>"),
                scope["method"],
                "cancelled" if cancelled else ("success" if status < 400 else "failure"),
                status,
                context.elapsed_ms(),
                operation,
                context.correlation_id,
                len(context.usage.attempts),
                context.error_code,
                meta.prompt_tokens if meta else None,
                meta.completion_tokens if meta else None,
                meta.reasoning_tokens if meta else None,
            )

    async def legacy(self, scope, receive, send):
        started = perf_counter()
        status = 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            return await self.app(scope, receive, capture)
        finally:
            logger.log(
                logging.INFO if status < 400 else logging.WARNING,
                "endpoint=%s method=%s outcome=%s status=%d duration_ms=%.2f",
                getattr(scope.get("route"), "name", "<unmatched>"),
                scope["method"],
                "success" if status < 400 else "failure",
                status,
                (perf_counter() - started) * 1000,
            )
