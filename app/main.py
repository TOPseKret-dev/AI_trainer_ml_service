import logging
from contextlib import asynccontextmanager
from time import perf_counter

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.clients.openrouter import OpenRouterClient
from app.errors import MLServiceError
from app.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ml_service.requests")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    async with httpx.AsyncClient() as http:
        app.state.openrouter_client = OpenRouterClient(http, settings)
        yield


app = FastAPI(
    title="InterSim ML Service",
    version="0.1.0",
    description="OpenRouter interviewer/evaluator; transcription and question bank are stubs.",
    lifespan=lifespan,
)


@app.exception_handler(MLServiceError)
async def handle_ml_error(request: Request, exc: MLServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.middleware("http")
async def log_request(request: Request, call_next) -> Response:
    started = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        # Log the matched endpoint name, never request bodies, filenames, or query strings.
        route = request.scope.get("route")
        logger.log(
            logging.INFO if status_code < 400 else logging.WARNING,
            "endpoint=%s method=%s outcome=%s status=%d duration_ms=%.2f",
            getattr(route, "name", "<unmatched>"),
            request.method,
            "success" if status_code < 400 else "failure",
            status_code,
            (perf_counter() - started) * 1000,
        )


app.include_router(health_router)
app.include_router(api_router, prefix="/api/v1")
