"""Run a company's research as several short conversations instead of one long one.

A branch is the ordinary agent loop pointed at one question. A single conversation
re-reads its own history every turn, so its cost grows with the square of its length;
the same work split across branches grows linearly. Budgets and the run ceiling are
enforced here rather than asked for in a prompt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

from scout.agent.plan import BRANCH_BRIEFS, BranchName, BranchPlan, ResearchPlan
from scout.agent.prompts import _as_url, system_prompt
from scout.agent.recorder import Recorder
from scout.agent.tools import (
    WEB_SEARCH_MAX_USES,
    WEB_SEARCH_MAX_USES_WITH_DOSSIER,
    build_tools,
    server_tools,
)
from scout.browser.session import BrowserSession
from scout.config import Settings
from scout.db import models as m
from scout.db.session import session_scope


@dataclass
class BranchOutcome:
    branch_id: int
    name: str
    depth: int
    status: str
    usage: Any
    summary: str | None = None
    error: str | None = None
    duration_s: float | None = None
    followups: list[dict] = field(default_factory=list)


def build_branch_brief(
    *,
    domain: str,
    name: str | None,
    branch: BranchPlan,
    requested_fields: list[str],
    icp: dict | None,
    allow_followups: bool,
    dossier: "Dossier | None" = None,
) -> str:
    """The user message for one branch.

    Narrow on purpose. A branch told about every field would drift into the others'
    territory and spend its budget twice over.
    """
    label = name or domain
    area = BranchName(branch.name)
    lines = [
        f"Research this company: {label}",
        f"Website: {_as_url(domain)}",
        "",
        f"## Your question ({area.value})",
        branch.question.strip(),
        "",
        "## What this area covers",
        BRANCH_BRIEFS[area],
        "",
    ]

    if requested_fields:
        lines += [
            "## Fields in scope for you",
            "Record these where your question turns them up, and mark the ones you "
            "cannot establish as unknown:",
            *[f"- {f}" for f in requested_fields],
            "",
        ]

    if icp:
        lines += [
            "## Ideal customer profile",
            "Note anything bearing on these, with evidence:",
            *[f"- {k}: {v}" for k, v in icp.items()],
            "",
        ]

    if dossier is not None and not dossier.is_empty():
        lines += ["## Already established, by branches that ran before you"]
        if dossier.facts:
            lines += [
                "Do not spend a search establishing these again:",
                *[f"- {f}: {v}" for f, v in dossier.facts],
                "",
                "One exception, and it matters: if a page you were reading anyway "
                "confirms one of these from a *different* site, record it as a "
                "finding with that site's own URL and quote. Each of these rests on "
                "a single source until a second one is recorded, and a second source "
                "found in passing is free. Do not go looking for one.",
                "",
            ]
        if dossier.urls:
            lines += [
                "Pages already found useful for this company. Open one directly "
                "rather than searching to find it again:",
                *[f"- {u}" for u in dossier.urls],
                "",
            ]
        if dossier.notes:
            lines += ["What those branches concluded:", *[f"- {n}" for n in dossier.notes], ""]

    lines += [
        "## Scope",
        "This question is your whole job. Other areas of this company are being "
        "researched separately, so do not stray into them -- duplicated work costs "
        "money twice and crowds out your own question.",
        "",
    ]

    if allow_followups:
        lines += [
            "If you come across another *organisation* whose own details would change "
            "the picture -- a parent, a subsidiary, an investor, a direct competitor "
            "-- call `note_followup` and move on. Do not research it yourself.",
            "",
        ]

    lines.append("Call `finish_research` as soon as your question is answered.")
    return "\n".join(lines)


def _create_branch_row(
    settings: Settings,
    *,
    company_run_id: int,
    branch: BranchPlan,
    depth: int,
    parent_branch_id: int | None,
) -> int:
    with session_scope(settings) as s:
        row = m.Branch(
            company_run_id=company_run_id,
            parent_branch_id=parent_branch_id,
            name=str(branch.name.value if hasattr(branch.name, "value") else branch.name),
            question=branch.question,
            depth=depth,
            status=m.Status.RUNNING.value,
        )
        s.add(row)
        s.flush()
        return row.id


def _finish_branch_row(settings: Settings, outcome: BranchOutcome, cost: float | None) -> None:
    with session_scope(settings) as s:
        row = s.get(m.Branch, outcome.branch_id)
        if row is None:
            return
        row.status = outcome.status
        row.summary = outcome.summary
        row.error = outcome.error
        row.duration_s = outcome.duration_s
        row.cost_usd = cost
        usage = outcome.usage
        if usage is not None:
            row.input_tokens = usage.input_tokens
            row.output_tokens = usage.output_tokens
            row.cache_read_tokens = usage.cache_read_tokens
            row.cache_write_tokens = usage.cache_write_tokens
            row.web_searches = usage.web_searches


def run_branch(
    *,
    client: Anthropic,
    settings: Settings,
    browser: BrowserSession | None,
    company_run_id: int,
    branch: BranchPlan,
    branch_id: int,
    depth: int,
    domain: str,
    name: str | None,
    requested_fields: list[str],
    icp: dict | None,
    budget_tokens: int,
    on_step: Any = None,
    dossier: "Dossier | None" = None,
    max_searches: int = WEB_SEARCH_MAX_USES,
) -> BranchOutcome:
    """One branch: one conversation, one question, one budget."""
    # Imported here because loop.py imports this module.
    from scout.agent.loop import (
        BETAS,
        MAX_TOKENS_PER_TURN,
        Usage,
        _harvest_server_tool_urls,
        context_management_arg,
    )

    started = time.monotonic()
    usage = Usage()
    recorder = Recorder(
        settings=settings,
        company_run_id=company_run_id,
        branch_id=branch_id,
        company_domain=domain,
    )
    tools = build_tools(session=browser, recorder=recorder, settings=settings)

    brief = build_branch_brief(
        domain=domain,
        name=name,
        branch=branch,
        requested_fields=requested_fields,
        icp=icp,
        allow_followups=settings.depth_profile.max_children > 0 and depth == 1,
        dossier=dossier,
    )

    status = m.Status.DONE.value
    error: str | None = None
    try:
        runner = client.beta.messages.tool_runner(
            model=settings.model,
            max_tokens=MAX_TOKENS_PER_TURN,
            cache_control={"type": "ephemeral"},
            system=[
                {
                    "type": "text",
                    "text": system_prompt(settings.engine),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": brief}],
            tools=[*tools, *server_tools(max_searches)],
            thinking={"type": "adaptive"},
            output_config={
                "effort": settings.effort,
                "task_budget": {"type": "tokens", "total": budget_tokens},
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
            _harvest_server_tool_urls(getattr(message, "content", None), recorder)
            if on_step is not None:
                on_step(message, recorder)
            if recorder.finished:
                break

        if getattr(final_message, "stop_reason", None) == "refusal":
            details = getattr(final_message, "stop_details", None)
            status = m.Status.BLOCKED.value
            error = (
                "model declined this branch "
                f"(category={getattr(details, 'category', None)})"
            )
    except Exception as exc:  # noqa: BLE001 - recorded on the branch, not swallowed
        # One branch failing must not lose the others' findings, which are already
        # written to the database by this point.
        status = m.Status.FAILED.value
        error = f"{type(exc).__name__}: {exc}"

    return BranchOutcome(
        branch_id=branch_id,
        name=str(branch.name.value if hasattr(branch.name, "value") else branch.name),
        depth=depth,
        status=status,
        usage=usage,
        summary=recorder.final_summary,
        error=error,
        duration_s=time.monotonic() - started,
        followups=list(recorder.followups),
    )


def child_plans(
    followups: list[dict], *, limit: int, already: set[str]
) -> list[BranchPlan]:
    """Turn noted organisations into child branches, de-duplicated and capped.

    Organisations only. A child branch asks what another company *is*, which is public
    corporate information; the same treatment of a named person would be building a
    profile of them, which §5 rules out.
    """
    plans: list[BranchPlan] = []
    for note in followups:
        entity = (note.get("entity") or "").strip()
        key = entity.lower()
        if not entity or key in already:
            continue
        already.add(key)
        plans.append(
            BranchPlan(
                name=BranchName.IDENTITY,
                question=(
                    f"Establish what {entity} is and how it relates to the company "
                    f"being researched ({note.get('kind', 'related organisation')}): "
                    "its legal identity, what it does, and its size. "
                    f"Context: {note.get('reason', '')}".strip()
                ),
                priority=1,
                rationale=f"Follow-up on {entity} ({note.get('kind')}).",
            )
        )
        if len(plans) >= limit:
            break
    return plans


def run_branches(
    *,
    client: Anthropic,
    settings: Settings,
    browser: BrowserSession | None,
    company_run_id: int,
    plan: ResearchPlan,
    domain: str,
    name: str | None,
    requested_fields: list[str],
    icp: dict | None,
    on_step: Any = None,
    on_branch: Any = None,
) -> list[BranchOutcome]:
    """Run a plan's branches in priority order, then any follow-ups they ask for.

    Stops when the run budget is exhausted. Branches are ordered by priority precisely
    so that running out of money drops the least important work rather than whatever
    happened to be last.
    """
    from scout.agent.pricing import estimate_cost_usd

    profile = settings.depth_profile
    per_branch = settings.task_budget_tokens
    ceiling = settings.run_budget_tokens

    outcomes: list[BranchOutcome] = []
    dossier = Dossier()
    spent = 0
    seen_entities: set[str] = set()
    queue: list[tuple[BranchPlan, int, int | None]] = [
        (b, 1, None) for b in sorted(plan.branches, key=lambda b: b.priority)
    ]
    children_run = 0

    while queue:
        branch, depth, parent_id = queue.pop(0)

        if spent >= ceiling:
            # Deliberately not an error: the run stops where the budget says it does,
            # and what has been recorded so far is still a usable profile.
            break

        branch_id = _create_branch_row(
            settings,
            company_run_id=company_run_id,
            branch=branch,
            depth=depth,
            parent_branch_id=parent_id,
        )
        if on_branch is not None:
            on_branch("start", branch, depth)

        outcome = run_branch(
            client=client,
            settings=settings,
            browser=browser,
            company_run_id=company_run_id,
            branch=branch,
            branch_id=branch_id,
            depth=depth,
            domain=domain,
            name=name,
            requested_fields=requested_fields,
            icp=icp,
            # Never let one branch spend what the whole run has left.
            budget_tokens=max(20_000, min(per_branch, ceiling - spent)),
            on_step=on_step,
            dossier=dossier,
            # The first branch has to find the company; the ones after it have been
            # told where it lives, so they are given fewer searches rather than
            # trusted to want fewer.
            max_searches=(
                WEB_SEARCH_MAX_USES
                if dossier.is_empty()
                else WEB_SEARCH_MAX_USES_WITH_DOSSIER
            ),
        )

        cost = estimate_cost_usd(model=settings.model, **outcome.usage.billable())
        _finish_branch_row(settings, outcome, cost)
        dossier = read_dossier(settings, company_run_id)
        outcomes.append(outcome)
        spent += outcome.usage.output_tokens + outcome.usage.cache_write_tokens
        if on_branch is not None:
            on_branch("done", branch, depth)

        if depth == 1 and outcome.followups and children_run < profile.max_children:
            for child in child_plans(
                outcome.followups,
                limit=profile.max_children - children_run,
                already=seen_entities,
            ):
                queue.append((child, 2, branch_id))
                children_run += 1

    return outcomes


def branch_rows(settings: Settings, company_run_id: int) -> list[dict]:
    """The tree as stored, for rendering and for the evaluation set."""
    with session_scope(settings) as s:
        rows = (
            s.query(m.Branch)
            .filter(m.Branch.company_run_id == company_run_id)
            .order_by(m.Branch.id)
            .all()
        )
        return [
            {
                "id": r.id,
                "parent_branch_id": r.parent_branch_id,
                "name": r.name,
                "question": r.question,
                "depth": r.depth,
                "status": r.status,
                "cost_usd": r.cost_usd,
                "duration_s": r.duration_s,
                "summary": r.summary,
                "error": r.error,
            }
            for r in rows
        ]


# --------------------------------------------------------------- branch handoff


@dataclass
class Dossier:
    """What earlier branches established, handed to the ones that follow.

    Branches are separate conversations on purpose -- that is what makes depth cost
    linearly instead of quadratically. The price is that each one starts ignorant, and
    the first thing an ignorant branch does is search for the company all over again.
    On one measured run that was ten searches for a single company: at a cent each,
    most of the bill, while every token of browsing and reasoning came to two cents.

    This is the cheap half of the fix. Anthropic runs ``web_search`` on its own servers,
    so the results never pass through this program and cannot be cached. What can be
    carried forward is what the searches *produced*: the established facts and the URLs
    worth reading. A branch given those can fetch directly instead of searching to find
    the same pages again.
    """

    facts: list[tuple[str, str]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.facts or self.urls)


# Kept small deliberately: the dossier enters every later branch's context, so a
# generous one would trade a search charge for a cache-write charge on every turn.
_DOSSIER_FACTS = 12
_DOSSIER_URLS = 10
_DOSSIER_VALUE_CHARS = 120


def read_dossier(settings: Settings, company_run_id: int) -> Dossier:
    """Collect what this company's finished branches have established so far."""
    with session_scope(settings) as s:
        findings = (
            s.query(m.Finding.field, m.Finding.value)
            .filter(m.Finding.company_run_id == company_run_id)
            .filter(m.Finding.kind == m.FindingKind.FACT.value)
            .order_by(m.Finding.id)
            .all()
        )
        urls = (
            s.query(m.Evidence.source_url)
            .join(m.Finding, m.Finding.id == m.Evidence.finding_id)
            .filter(m.Finding.company_run_id == company_run_id)
            .order_by(m.Evidence.id)
            .all()
        )
        summaries = (
            s.query(m.Branch.name, m.Branch.summary)
            .filter(m.Branch.company_run_id == company_run_id)
            .filter(m.Branch.status == m.Status.DONE.value)
            .order_by(m.Branch.id)
            .all()
        )

    facts: list[tuple[str, str]] = []
    seen_fields: set[str] = set()
    for field_name, raw in findings:
        # One value per field: the point is to stop a later branch re-establishing
        # something, not to replay every variant of it.
        if field_name in seen_fields:
            continue
        value = _trim(raw)
        if not value:
            continue
        seen_fields.add(field_name)
        facts.append((field_name, value))

    seen_urls: set[str] = set()
    kept_urls: list[str] = []
    for (url,) in urls:
        if url and url not in seen_urls:
            seen_urls.add(url)
            kept_urls.append(url)

    notes = [
        f"{name}: {_trim(summary, 200)}" for name, summary in summaries if summary
    ]
    return Dossier(
        facts=facts[:_DOSSIER_FACTS],
        urls=kept_urls[:_DOSSIER_URLS],
        notes=notes,
    )


def _trim(raw: Any, limit: int = _DOSSIER_VALUE_CHARS) -> str:
    """Values are stored as JSON, so a string arrives quoted."""
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip().strip('"')
    text = " ".join(text.split())
    return text[: limit - 1] + "…" if len(text) > limit else text
