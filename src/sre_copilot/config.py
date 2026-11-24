"""Runtime configuration, loaded from ``SRE_COPILOT_*`` environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SRE_COPILOT_", env_file=".env", extra="ignore", env_nested_delimiter="__"
    )

    # --- LLM -------------------------------------------------------------------------------
    llm: Literal["anthropic", "replay"] = "anthropic"
    model: str = "claude-sonnet-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens_per_turn: int = 16_000
    refusal_fallback: bool = True
    replay_script: Path | None = None
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 3

    # --- Budget ----------------------------------------------------------------------------
    max_steps: int = 16
    max_tool_calls: int = 40
    max_total_tokens: int = 400_000
    max_wall_seconds: float = 300.0

    # --- Tool backends ---------------------------------------------------------------------
    tools: Literal["live", "fixtures"] = "live"
    fixtures_path: Path | None = None
    record_path: Path | None = None
    prometheus_url: str = "http://localhost:39090"
    prometheus_public_url: str | None = None
    loki_url: str = "http://localhost:39100"
    loki_public_url: str | None = None
    kubeconfig_read: Path | None = None
    kubeconfig_write: Path | None = None
    kube_context: str | None = None
    git_repo: Path | None = None
    runbooks_dir: Path = Path("runbooks")
    http_timeout_seconds: float = 10.0
    max_tool_output_chars: int = 6_000

    # --- Safety ----------------------------------------------------------------------------
    allowed_namespaces: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["demo"])
    max_scale_replicas: int = 20

    # --- Approval gate ---------------------------------------------------------------------
    state_dir: Path = Path(".sre-copilot")
    approval_ttl_seconds: int = 900
    approval_signing_secret: SecretStr | None = None
    alertmanager_token: SecretStr | None = None
    notify_webhook_url: str | None = None
    verify_timeout_seconds: float = 180.0

    @field_validator("allowed_namespaces", mode="before")
    @classmethod
    def _split_namespaces(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def db_path(self) -> Path:
        return self.state_dir / "copilot.db"

    @property
    def audit_log_path(self) -> Path:
        return self.state_dir / "audit.jsonl"


def load_settings(**overrides: object) -> Settings:
    """Build settings from the environment, applying explicit (non-None) overrides."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return Settings(**clean)  # type: ignore[arg-type]
