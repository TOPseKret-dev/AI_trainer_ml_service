from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["openrouter", "openai"] = "openrouter"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = Field(default="gpt-6.2-sol", min_length=3)
    openai_base_url: AnyHttpUrl = AnyHttpUrl("https://api.openai.com/v1")
    openai_timeout_seconds: float = Field(default=55, gt=0, allow_inf_nan=False)
    openai_max_output_tokens: int = Field(default=8000, ge=128, le=32000)
    openai_reasoning_effort: Literal["low", "medium", "high"] | None = "low"

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: AnyHttpUrl = AnyHttpUrl("https://openrouter.ai/api/v1")
    openrouter_model: Literal["deepseek/deepseek-v4.1-flash", "qwen/qwen3.5-27b"] = (
        "deepseek/deepseek-v4.1-flash"
    )
    openrouter_provider: Literal["deepinfra/fp8", "alibaba"] = "deepinfra/fp8"
    openrouter_timeout_seconds: float = Field(default=55, gt=0, allow_inf_nan=False)
    llm_request_timeout_seconds: float = Field(default=55, gt=0, le=55, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_provider_route(self) -> Self:
        providers = {"deepseek/deepseek-v4.1-flash": "deepinfra/fp8", "qwen/qwen3.5-27b": "alibaba"}
        if self.llm_provider == "openrouter" and providers[self.openrouter_model] != self.openrouter_provider:
            raise ValueError("Set OPENROUTER_MODEL and OPENROUTER_PROVIDER to a supported pair")
        return self
