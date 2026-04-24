"""Settings loader (pydantic-settings).

Traces to: seed-spec.md §6 (Config schema), workplan.md Phase 1.

Phase 0 scaffolding: minimal Settings with env_prefix="" (supports bare
DISCORD_TOKEN env var per SEC-P0-01). YAML config loading is wired in Phase 1.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level settings. Phase 0 stub — expanded in Phase 1."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = Field(default="info")
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".discord-scanner")
    discord_token: str | None = Field(default=None)
