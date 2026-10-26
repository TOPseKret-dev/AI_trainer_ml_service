from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: AnyHttpUrl = AnyHttpUrl("https://openrouter.ai/api/v1")
    # Deliberately pinned: changing the model/provider requires capability verification.
    openrouter_model: Literal["qwen/qwen3.5-27b"] = "qwen/qwen3.5-27b"
    openrouter_provider: Literal["alibaba"] = "alibaba"
    openrouter_timeout_seconds: float = Field(default=55, gt=0, allow_inf_nan=False)
