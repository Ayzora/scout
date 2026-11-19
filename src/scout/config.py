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

# "browser" drives a real Chromium; "fetch" has no browser and uses only the
# server-side web tools. See build_tools in agent/tools.py.
VALID_ENGINES = ("browser", "fetch")
DEFAULT_ENGINE = "browser"


@dataclass(frozen=True)
class DepthProfile:
    """How wide and how deep a run is allowed to go."""

    max_branches: int
    max_children: int
    description: str


# Depth is a count of branches, not a bigger budget for one conversation. A single
# conversation re-reads its whole history every turn, so its cost grows with the square
# of its length; the same work split across short branches grows linearly.
DEPTH_PROFILES = {
    "shallow": DepthProfile(1, 0, "one pass, as before the tree existed"),
    "standard": DepthProfile(4, 0, "the main areas, researched separately"),
    "deep": DepthProfile(6, 4, "every area, plus follow-ups on what it finds"),
}
VALID_DEPTHS = tuple(DEPTH_PROFILES)
DEFAULT_DEPTH = "standard"


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
    engine: str
    depth: str
    synthesis_model: str
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

    @property
    def depth_profile(self) -> DepthProfile:
        return DEPTH_PROFILES[self.depth]

    @property
    def run_budget_tokens(self) -> int:
        """Ceiling for a whole company, across every branch.

        ``task_budget_tokens`` is per branch, so without a ceiling a deep run's cost
        would be the branch budget times however many branches the planner asked for.
        """
        profile = self.depth_profile
        return self.task_budget_tokens * (profile.max_branches + profile.max_children)

    @staticmethod
    def api_key() -> str | None:
        """Read lazily, so commands that need no API key still run without one.

        The ``.env.example`` placeholder counts as absent, so a fresh install reports
        a missing key rather than an authentication error.
        """
        raw = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not raw or "..." in raw:
            return None
        return raw


def load_settings() -> Settings:
    model = os.getenv("SCOUT_MODEL", "claude-haiku-5-5").strip()
    effort = os.getenv("SCOUT_EFFORT", "medium").strip().lower()
    if effort not in VALID_EFFORTS:
        raise ValueError(
            f"SCOUT_EFFORT must be one of {', '.join(VALID_EFFORTS)}, got {effort!r}"
        )

    engine = os.getenv("SCOUT_ENGINE", DEFAULT_ENGINE).strip().lower()
    if engine not in VALID_ENGINES:
        raise ValueError(
            f"SCOUT_ENGINE must be one of {', '.join(VALID_ENGINES)}, got {engine!r}"
        )

    depth = os.getenv("SCOUT_DEPTH", DEFAULT_DEPTH).strip().lower()
    if depth not in VALID_DEPTHS:
        raise ValueError(
            f"SCOUT_DEPTH must be one of {', '.join(VALID_DEPTHS)}, got {depth!r}"
        )

    # The main scope throttle: a larger budget means the agent keeps researching
    # past the objective.
    budget = _env_int("SCOUT_TASK_BUDGET_TOKENS", 40_000)
    if budget < MIN_TASK_BUDGET_TOKENS:
        raise ValueError(
            f"SCOUT_TASK_BUDGET_TOKENS must be at least {MIN_TASK_BUDGET_TOKENS}, got {budget}"
        )

    data_dir = Path(os.getenv("SCOUT_DATA_DIR", "./data")).expanduser().resolve()

    return Settings(
        model=model,
        effort=effort,
        engine=engine,
        depth=depth,
        synthesis_model=os.getenv("SCOUT_SYNTHESIS_MODEL", model).strip(),
        task_budget_tokens=budget,
        headed=_env_bool("SCOUT_HEADED", True),
        # Off deliberately -- see context_management_arg in agent/loop.py.
        context_editing=_env_bool("SCOUT_CONTEXT_EDITING", False),
        max_steps=_env_int("SCOUT_MAX_STEPS", 60),
        page_timeout_ms=_env_int("SCOUT_PAGE_TIMEOUT_MS", 20_000),
        data_dir=data_dir,
    )
