"""Bound ASR uploads before multipart parsing and hold reservations until completion."""

import asyncio

from starlette.responses import JSONResponse


class AsrAdmission:
    def __init__(self, app, max_requests=14, max_bytes=32 * 1024 * 1024):
        self.app = app
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.active = 0
        self.bytes = 0

    async def reject(self, scope, receive, send, code, status):
        response = JSONResponse(
            {
                "error": {
                    "code": code,
                    "message": code,
                    "details": {"retryable": status in (503, 504)},
                }
            },
            status_code=status,
        )
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].rstrip("/") != "/api/v1/transcriptions":
            return await self.app(scope, receive, send)
        if self.active >= self.max_requests:
            return await self.reject(scope, receive, send, "ASR_QUEUE_FULL", 503)
        self.active += 1
        reserved = 0
        chunks = []
        try:
            try:
                async with asyncio.timeout(30):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if reserved + len(chunk) > 25000000 + 65536:
                            return await self.reject(scope, receive, send, "PAYLOAD_TOO_LARGE", 413)
                        if self.bytes + len(chunk) > self.max_bytes:
                            return await self.reject(scope, receive, send, "ASR_QUEUE_FULL", 503)
                        reserved += len(chunk)
                        self.bytes += len(chunk)
                        chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await self.reject(scope, receive, send, "ASR_TIMEOUT", 504)

            # Replay in chunks, retaining the byte reservation through provider completion.
            iterator = iter(chunks)

            async def replay():
                nonlocal iterator
                if iterator is None:
                    return await receive()
                try:
                    return {"type": "http.request", "body": next(iterator), "more_body": True}
                except StopIteration:
                    iterator = None
                    chunks.clear()
                    return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay, send)
        finally:
            self.active -= 1
            self.bytes -= reserved
