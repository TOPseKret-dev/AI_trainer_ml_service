from typing import Literal, Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
        if providers[self.openrouter_model] != self.openrouter_provider:
            raise ValueError("Set OPENROUTER_MODEL and OPENROUTER_PROVIDER to a supported pair")
        return self
