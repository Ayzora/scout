"""Runtime configuration, loaded from the environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Minimum the API accepts for an advisory task budget.
MIN_TASK_BUDGET_TOKENS = 20_000

VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Everything Scout needs to run, resolved once at startup."""

    model: str
    effort: str
    task_budget_tokens: int
    headed: bool
    context_editing: bool
    max_steps: int
    page_timeout_ms: int
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scout.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+pysqlite:///{self.db_path}"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @staticmethod
    def api_key() -> str | None:
        """Read lazily, so commands that need no API key still run without one.

        The placeholder shipped in ``.env.example`` is treated as absent. Without
        this, a fresh install (where install.sh copies the example file) would send
        ``sk-ant-...`` to the API and surface an authentication error instead of the
        message telling the user to add their key.
        """
        raw = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not raw or "..." in raw:
            return None
        return raw


def load_settings() -> Settings:
    effort = os.getenv("SCOUT_EFFORT", "high").strip().lower()
    if effort not in VALID_EFFORTS:
        raise ValueError(
            f"SCOUT_EFFORT must be one of {', '.join(VALID_EFFORTS)}, got {effort!r}"
        )

    budget = _env_int("SCOUT_TASK_BUDGET_TOKENS", 120_000)
    if budget < MIN_TASK_BUDGET_TOKENS:
        raise ValueError(
            f"SCOUT_TASK_BUDGET_TOKENS must be at least {MIN_TASK_BUDGET_TOKENS}, got {budget}"
        )

    data_dir = Path(os.getenv("SCOUT_DATA_DIR", "./data")).expanduser().resolve()

    return Settings(
        model=os.getenv("SCOUT_MODEL", "claude-opus-5-5").strip(),
        effort=effort,
        task_budget_tokens=budget,
        headed=_env_bool("SCOUT_HEADED", True),
        # Off by default: it fights prompt caching and only earns its keep near the
        # context window. See context_management_arg in agent/loop.py.
        context_editing=_env_bool("SCOUT_CONTEXT_EDITING", False),
        max_steps=_env_int("SCOUT_MAX_STEPS", 60),
        page_timeout_ms=_env_int("SCOUT_PAGE_TIMEOUT_MS", 20_000),
        data_dir=data_dir,
    )
