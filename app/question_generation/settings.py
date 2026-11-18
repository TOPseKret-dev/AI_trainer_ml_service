"""Server-owned limits. These never come from the admin's generation request."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    model: str = Field(min_length=3, max_length=128, pattern=r"^\S+/\S+$")
    input_usd_per_million: float = Field(gt=0, le=100)
    output_usd_per_million: float = Field(gt=0, le=100)


class GenerationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    writer: ModelPolicy = ModelPolicy(
        model="anthropic/claude-sonnet-5.5", input_usd_per_million=2.5, output_usd_per_million=12.0
    )
    fallback_writer: ModelPolicy = ModelPolicy(
        model="google/gemini-3.8-flash", input_usd_per_million=1.0, output_usd_per_million=5.0
    )
    reviewer: ModelPolicy = ModelPolicy(
        model="openai/gpt-5.6-luna", input_usd_per_million=0.5, output_usd_per_million=2.5
    )
    plan_slots: bool = True
    semantic_deduplication: bool = True
    max_cost_usd: float = Field(default=1.0, gt=0, le=10)
    deadline_seconds: float = Field(default=900, gt=0, le=3600)
    call_timeout_seconds: float = Field(default=90, gt=0, le=300)
    max_rounds: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    attempts_per_stage: Annotated[int, Field(strict=True, ge=1, le=3)] = 2
    writer_tokens: Annotated[int, Field(strict=True, ge=1000, le=16000)] = 9000
    reviewer_tokens: Annotated[int, Field(strict=True, ge=1000, le=8000)] = 4000
    max_request_bytes: Annotated[int, Field(strict=True, ge=10000, le=300000)] = 180000

    @model_validator(mode="after")
    def independent(self):
        if self.reviewer.model.split("/")[0] in {
            self.writer.model.split("/")[0],
            self.fallback_writer.model.split("/")[0],
        }:
            raise ValueError("reviewer must belong to a different model family than both writers")
        return self
