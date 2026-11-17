"""Loads and validates config.yml.

The file is re-read every time a new session is created, so edits to
config.yml take effect for the next session without restarting anything.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config.yml"))

ParticipantType = Literal["human", "ai"]


class ExperimentConfig(BaseModel):
    rounds: int = Field(2, ge=1, le=10)
    chat_duration_seconds: int = Field(60, ge=5)
    vote_duration_seconds: int = Field(20, ge=5)
    results_display_seconds: int = Field(10, ge=1)
    start_countdown_seconds: int = Field(5, ge=0)
    randomize_seats: bool = True
    max_message_length: int = Field(300, ge=20, le=2000)
    # If every human disconnects mid-session and nobody returns within this
    # many seconds, the session is aborted (stored with status "aborted").
    abort_after_all_humans_left_seconds: float = Field(15, ge=0)
    topic: str = (
        "Discuss who among Player A, Player B and Player C should be elected "
        "'Decision Maker'. In the next experiment the Decision Maker will make "
        "choices on behalf of the whole group."
    )


class AITimingConfig(BaseModel):
    """All values are seconds; pairs are [min, max] for a uniform random draw."""

    join_delay: tuple[float, float] = (3.0, 12.0)
    first_message_delay: tuple[float, float] = (4.0, 14.0)
    idle_check_interval: tuple[float, float] = (8.0, 16.0)
    read_delay: tuple[float, float] = (1.0, 3.5)
    typing_chars_per_second: float = Field(5.5, gt=0)
    min_gap_between_messages: float = 6.0
    max_messages_per_round: int = 5
    vote_delay: tuple[float, float] = (3.0, 12.0)


class AIConfig(BaseModel):
    provider: Literal["anthropic", "rules", "mock"] = "anthropic"  # "mock" = old name for "rules"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    max_tokens: int = 4000
    use_fallbacks: bool = True
    request_timeout_seconds: float = 30.0
    api_key_env: str = "ANTHROPIC_API_KEY"
    timing: AITimingConfig = AITimingConfig()
    personas: list[str] = Field(
        default_factory=lambda: [
            "friendly and a bit chatty, types in lowercase, sometimes skips punctuation",
            "pragmatic and brief, cares about who seems reliable",
            "slightly skeptical, asks others questions before deciding",
        ]
    )


class AppConfig(BaseModel):
    condition: str
    conditions: dict[str, list[ParticipantType]]
    experiment: ExperimentConfig = ExperimentConfig()
    ai: AIConfig = AIConfig()

    @model_validator(mode="after")
    def _check(self) -> "AppConfig":
        for name, slots in self.conditions.items():
            if len(slots) != 3:
                raise ValueError(f"condition '{name}' must list exactly 3 participants")
            if "human" not in slots:
                raise ValueError(f"condition '{name}' needs at least one human")
        return self

    def active_condition(self) -> str:
        return os.environ.get("EXPERIMENT_CONDITION") or self.condition

    def participants(self) -> list[ParticipantType]:
        name = self.active_condition()
        if name not in self.conditions:
            raise ValueError(
                f"unknown condition '{name}'; choose one of {sorted(self.conditions)}"
            )
        return list(self.conditions[name])


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return AppConfig.model_validate(raw)
