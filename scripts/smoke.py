"""Exercise a running service without invoking a paid model or mutating stored data."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def check(base_url: str) -> None:
    def request(path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode()
        req = Request(
            base_url.rstrip("/") + path,
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urlopen(req, timeout=10) as response:
            return json.load(response)

    for path in ("/health", "/health/live"):
        if request(path) != {"status": "ok", "service": "ml-service"}:
            raise ValueError(f"Unexpected liveness response: {path}")
        print(f"OK {path}")

    if request("/health/ready") != {"status": "ready", "service": "ml-service"}:
        raise ValueError("Service is not configured for LLM requests")
    print("OK /health/ready (local configuration only)")

    paths = request("/openapi.json")["paths"]
    required = {
        "/api/v1/interviewer",
        "/api/v1/evaluator",
        "/api/v1/transcriptions",
        "/api/v1/question-bank/generate",
    }
    if not required.issubset(paths):
        raise ValueError("OpenAPI is missing a required route")
    print("OK /openapi.json")

    # The hard limit is evaluated in code, before any provider request.
    result = request(
        "/api/v1/interviewer",
        {
            "question": "Deployment smoke question",
            "criteria": [{"criterion_id": "smoke", "description": "Deployment smoke criterion"}],
            "dialogue": [{"role": "candidate", "content": "Deployment smoke answer"}],
            "follow_up_count": 0,
            "max_follow_ups": 0,
        },
    )
    if result != {"action": "complete", "message": None}:
        raise ValueError("Interviewer hard limit did not complete the question")
    print("OK interviewer hard limit (no inference)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    try:
        check(args.base_url)
    except HTTPError as exc:
        print(f"Smoke failed: HTTP {exc.code}; check service readiness and logs", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, ValueError, KeyError) as exc:
        print(f"Smoke failed: {type(exc).__name__}; check service URL and logs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
