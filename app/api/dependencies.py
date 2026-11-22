from fastapi import Request

from app.clients.openai import OpenAIClient
from app.clients.openrouter import OpenRouterClient


def get_openrouter_client(request: Request) -> OpenRouterClient | OpenAIClient:
    return request.app.state.openrouter_client
