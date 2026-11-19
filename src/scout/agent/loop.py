"""Runs the research agent for one company.

The loop itself is the SDK's tool runner; this module supplies the tools, keeps the
database in step, and accumulates the metrics the evaluation report needs.

Two details worth knowing:

* We iterate the runner rather than calling ``until_done()``, because each iteration
  hands us that turn's message and therefore that turn's token usage. Cost per company
  is a stated success metric, so it has to be measured rather than estimated afterwards.
* Server-side tools (web search, web fetch) run on Anthropic's infrastructure, so the
  URLs they touch never pass through our tool functions. We harvest them from the
  response blocks so the provenance guard does not flag a legitimately searched page.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from anthropic import Anthropic

from scout.agent.prompts import DEFAULT_FIELDS, build_company_brief, system_prompt
from scout.agent.pricing import estimate_cost_usd
from scout.agent.recorder import Recorder
from scout.agent.schemas import SynthesisResult
from scout.agent.synthesize import synthesize
from scout.agent.tools import SERVER_TOOLS, build_tools
from scout.browser.session import BrowserSession
from scout.config import Settings
from scout.db import models as m
from scout.db.session import session_scope

BETAS = [
    "task-budgets-2026-03-13",
    "context-management-2025-06-27",
    "server-side-fallback-2026-07-01",
]


@contextmanager
def _browser_for(settings: Settings, shot_dir: Path) -> Iterator[BrowserSession | None]:
    """A browser for the browser engine, nothing for the fetch engine."""
    if settings.engine != "browser":
        yield None
        return
    with BrowserSession(
        screenshot_dir=shot_dir,
        headed=settings.headed,
        timeout_ms=settings.page_timeout_ms,
    ) as browser:
        yield browser


def context_management_arg(settings: Settings) -> dict[str, Any]:
    """Context editing, off unless explicitly enabled.

    It makes room in the context window; it does not save money. Each clearing pass
    rewrites the conversation the cache was built from, so the next turn pays to write
    it again. Only worth enabling for runs that approach the window, with a high
    trigger so clears stay rare.
    """
    if not settings.context_editing:
        return {}
    return {
        "context_management": {
            "edits": [
                {
                    "type": "clear_tool_uses_20250919",
                    "trigger": {"type": "input_tokens", "value": 350_000},
                    "clear_at_least": {"type": "input_tokens", "value": 50_000},
                }
            ]
        }
    }

# Each assistant turn in an agent loop is short (thinking plus a tool call), so this
# does not need to be large. The overall run is bounded by the task budget instead.
MAX_TOKENS_PER_TURN = 24_000


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    turns: int = 0
    web_searches: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        self.turns += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        # Billed per request, not per token.
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            self.web_searches += getattr(server, "web_search_requests", 0) or 0

    def merge(self, other: "Usage") -> "Usage":
        """Combine two phases into one total."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            turns=self.turns + other.turns,
            web_searches=self.web_searches + other.web_searches,
        )

    def billable(self) -> dict[str, int]:
        """The fields estimate_cost_usd prices."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "web_searches": self.web_searches,
        }

    def as_dict(self) -> dict[str, int]:
        return {**self.billable(), "turns": self.turns}


@dataclass
class CompanyResult:
    company_run_id: int
    domain: str
    status: str
    stats: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    duration_s: float | None = None
    synthesis: SynthesisResult | None = None
    error: str | None = None
    stop_reason: str | None = None


def _harvest_server_tool_urls(content: Any, recorder: Recorder) -> None:
    """Record URLs that web_search / web_fetch visited on Anthropic's side."""
    for block in content or []:
        btype = getattr(block, "type", None)
        if btype == "web_search_tool_result":
            inner = getattr(block, "content", None)
            if isinstance(inner, list):
                for item in inner:
                    url = getattr(item, "url", None)
                    if url:
                        recorder.fetched_urls.add(url)
        elif btype == "web_fetch_tool_result":
            inner = getattr(block, "content", None)
            url = getattr(inner, "url", None)
            if url:
                recorder.fetched_urls.add(url)


def ensure_company(settings: Settings, domain: str, name: str | None) -> int:
    with session_scope(settings) as s:
        company = s.query(m.Company).filter(m.Company.domain == domain).one_or_none()
        if company is None:
            company = m.Company(domain=domain, name=name)
            s.add(company)
            s.flush()
        elif name and not company.name:
            company.name = name
        return company.id


def create_run(
    settings: Settings,
    *,
    label: str | None,
    objectives: str | None,
    requested_fields: list[str] | None,
    icp: dict | None,
) -> int:
    with session_scope(settings) as s:
        run = m.Run(
            label=label,
            objectives=objectives,
            requested_fields=requested_fields or DEFAULT_FIELDS,
            icp=icp,
            model=settings.model,
            effort=settings.effort,
            engine=settings.engine,
            status=m.Status.RUNNING.value,
        )
        s.add(run)
        s.flush()
        return run.id


def research_company(
    *,
    client: Anthropic,
    settings: Settings,
    run_id: int,
    domain: str,
    name: str | None = None,
    objectives: str | None = None,
    requested_fields: list[str] | None = None,
    icp: dict | None = None,
    on_step: Any = None,
) -> CompanyResult:
    """Research one company end to end and persist everything."""
    company_id = ensure_company(settings, domain, name)

    with session_scope(settings) as s:
        company_run = m.CompanyRun(
            run_id=run_id,
            company_id=company_id,
            status=m.Status.RUNNING.value,
            started_at=datetime.now(timezone.utc),
        )
        s.add(company_run)
        s.flush()
        company_run_id = company_run.id

    shot_dir = settings.screenshot_dir / f"run-{run_id}" / f"company-{company_run_id}"
    started = time.monotonic()
    usage = Usage()
    synthesis_usage = Usage()
    result = CompanyResult(
        company_run_id=company_run_id, domain=domain, status=m.Status.RUNNING.value
    )

    try:
        with _browser_for(settings, shot_dir) as browser:
            recorder = Recorder(settings=settings, company_run_id=company_run_id)
            tools = build_tools(
                session=browser, recorder=recorder, settings=settings
            )

            brief = build_company_brief(
                domain=domain,
                name=name,
                objectives=objectives,
                requested_fields=requested_fields or DEFAULT_FIELDS,
                icp=icp,
            )

            runner = client.beta.messages.tool_runner(
                model=settings.model,
                max_tokens=MAX_TOKENS_PER_TURN,
                # Caches the growing conversation tail. Required in addition to the
                # system breakpoint below: without it every turn re-reads the whole
                # conversation at the full input rate.
                cache_control={"type": "ephemeral"},
                system=[
                    {
                        "type": "text",
                        "text": system_prompt(settings.engine),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": brief}],
                tools=[*tools, *SERVER_TOOLS],
                thinking={"type": "adaptive"},
                output_config={
                    "effort": settings.effort,
                    "task_budget": {
                        "type": "tokens",
                        "total": settings.task_budget_tokens,
                    },
                },
                **context_management_arg(settings),
                fallbacks="default",
                betas=BETAS,
                max_iterations=settings.max_steps + 10,
                stream=True,
            )

            final_message = None
            for stream in runner:
                message = stream.get_final_message()
                final_message = message
                usage.add(getattr(message, "usage", None))
                _harvest_server_tool_urls(
                    getattr(message, "content", None), recorder
                )
                if on_step is not None:
                    on_step(message, recorder)
                if recorder.finished:
                    break

            result.stop_reason = getattr(final_message, "stop_reason", None)
            if result.stop_reason == "refusal":
                details = getattr(final_message, "stop_details", None)
                result.error = (
                    "model declined the request "
                    f"(category={getattr(details, 'category', None)})"
                )

            result.stats = recorder.stats()

            # Synthesis sees only what was recorded, never the live pages.
            findings = recorder.snapshot_of_findings()
            if findings:
                result.synthesis, synth_usage = synthesize(
                    client=client,
                    settings=settings,
                    findings=findings,
                    people=recorder.snapshot_of_people(),
                    objectives=objectives,
                    icp=icp,
                    research_summary=recorder.final_summary,
                )
                synthesis_usage.add(synth_usage)

            result.status = (
                m.Status.DONE.value if result.error is None else m.Status.FAILED.value
            )

    except Exception as exc:  # noqa: BLE001 - surfaced on the record, not swallowed
        result.status = m.Status.FAILED.value
        result.error = f"{type(exc).__name__}: {exc}"

    result.duration_s = time.monotonic() - started
    total = usage.merge(synthesis_usage)
    result.usage = total.as_dict()
    result.usage["research_cost_usd"] = estimate_cost_usd(
        model=settings.model, **usage.billable()
    )
    # Priced on its own model, which may differ from the research model.
    result.usage["synthesis_cost_usd"] = estimate_cost_usd(
        model=settings.synthesis_model, **synthesis_usage.billable()
    )
    research_cost = result.usage["research_cost_usd"]
    synthesis_cost = result.usage["synthesis_cost_usd"]
    result.cost_usd = (
        None
        if research_cost is None or synthesis_cost is None
        else research_cost + synthesis_cost
    )

    _persist_company_run(settings, result)
    return result


def _persist_company_run(settings: Settings, result: CompanyResult) -> None:
    with session_scope(settings) as s:
        company_run = s.get(m.CompanyRun, result.company_run_id)
        if company_run is None:
            return
        company_run.status = result.status
        company_run.finished_at = datetime.now(timezone.utc)
        company_run.duration_s = result.duration_s
        company_run.input_tokens = result.usage.get("input_tokens", 0)
        company_run.output_tokens = result.usage.get("output_tokens", 0)
        company_run.cache_read_tokens = result.usage.get("cache_read_tokens", 0)
        company_run.cache_write_tokens = result.usage.get("cache_write_tokens", 0)
        company_run.cost_usd = result.cost_usd
        company_run.step_count = result.stats.get("steps", 0)
        company_run.error = result.error

        if result.synthesis is not None:
            company_run.summary = result.synthesis.profile.summary
            q = result.synthesis.qualification
            if q is not None:
                s.add(
                    m.Qualification(
                        company_run_id=company_run.id,
                        score=q.score,
                        tier=q.tier,
                        rationale=q.rationale,
                        icp_match={
                            "matched": q.matched_criteria,
                            "missed": q.missed_criteria,
                        },
                    )
                )


def finish_run(settings: Settings, run_id: int) -> None:
    with session_scope(settings) as s:
        run = s.get(m.Run, run_id)
        if run is None:
            return
        failed = (
            s.query(m.CompanyRun)
            .filter(
                m.CompanyRun.run_id == run_id,
                m.CompanyRun.status == m.Status.FAILED.value,
            )
            .count()
        )
        total = (
            s.query(m.CompanyRun).filter(m.CompanyRun.run_id == run_id).count()
        )
        run.status = (
            m.Status.FAILED.value
            if failed == total and total > 0
            else m.Status.DONE.value
        )
        run.finished_at = datetime.now(timezone.utc)
