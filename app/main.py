import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.asr_admission import AsrAdmission
from app.clients.openai import OpenAIClient
from app.clients.openrouter import OpenRouterClient
from app.errors import MLErrorCode, MLServiceError
from app.http_contract import MLContractMiddleware, error_response, get_context
from app.question_generation_http import QuestionGenerationMiddleware
from app.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ml_service.requests")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    async with httpx.AsyncClient() as http:
        app.state.openrouter_client = (
            OpenAIClient(http, settings)
            if settings.llm_provider == "openai"
            else OpenRouterClient(http, settings)
        )
        try:
            yield
        finally:
            # Cached generation service borrows this lifespan's HTTP client.
            if hasattr(app.state, "question_generation_service"):
                del app.state.question_generation_service


app = FastAPI(
    title="InterSim ML Service",
    version="0.2.0",
    description=(
        "Selectable OpenAI/OpenRouter interviewer/evaluator; configurable ASR; draft generation. "
        "The original question-bank stub remains available for compatibility."
    ),
    lifespan=lifespan,
)


@app.exception_handler(MLServiceError)
async def handle_ml_error(request: Request, exc: MLServiceError) -> JSONResponse:
    context = get_context(request)
    if context is not None:
        return error_response(context, exc)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError):
    context = get_context(request)
    if context is not None:
        return error_response(context, MLServiceError(MLErrorCode.INVALID_INPUT))
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(HTTPException)
async def handle_http_error(request: Request, exc: HTTPException):
    context = get_context(request)
    if context is not None:
        return error_response(context, MLServiceError(MLErrorCode.INVALID_INPUT))
    return await http_exception_handler(request, exc)


app.include_router(health_router)
app.include_router(api_router, prefix="/api/v1")

app.add_middleware(AsrAdmission)
app.add_middleware(QuestionGenerationMiddleware)
app.add_middleware(MLContractMiddleware)
