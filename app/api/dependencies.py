from fastapi import Request

from app.clients.openrouter import OpenRouterClient


def get_openrouter_client(request: Request) -> OpenRouterClient:
    return request.app.state.openrouter_client
