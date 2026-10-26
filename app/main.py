import logging
from time import perf_counter

from fastapi import FastAPI, Request, Response

from app.api.router import api_router
from app.api.routes.health import router as health_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ml_service.requests")

app = FastAPI(
    title="InterSim ML Service",
    version="0.1.0",
    description="Stateless ML HTTP service. All four inference functions are deterministic stubs.",
)


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
