"""Choose which areas of a company to research, in one structured call.

Planning is cheap and has no tools, so the expensive agentic loops only ever run
against a decided question. Identity and people are not optional: a profile without
either is not a lead, so they are filled in here when the planner omits them.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from anthropic import Anthropic
from pydantic import BaseModel, Field

from scout.config import Settings


class BranchName(str, Enum):
    """The areas a company can be researched in.

    Deliberately few and non-overlapping, so two branches do not spend budget on the
    same pages. "people" covers who runs it; "signals" covers what has changed lately.
    """

    IDENTITY = "identity"
    PRODUCTS = "products"
    PEOPLE = "people"
    FINANCIALS = "financials"
    MARKET = "market"
    SIGNALS = "signals"


BRANCH_BRIEFS = {
    BranchName.IDENTITY: (
        "The legal and factual identity of the company: registered name and number, "
        "legal entity, headquarters and other locations, corporate structure, "
        "regulators. Statutory registers outrank the company's own description."
    ),
    BranchName.PRODUCTS: (
        "What the company actually sells, to whom, and how it is packaged or priced. "
        "One entry per product or service rather than one long string."
    ),
    BranchName.PEOPLE: (
        "Who runs the company: named leadership and their roles, as published by the "
        "company itself or in a public filing. No email guessing, no personal "
        "contact details, nothing from a contact database."
    ),
    BranchName.FINANCIALS: (
        "Size and money: revenue, profit, funding rounds, investors, headcount, "
        "filed accounts. Prefer filings and the company's own reporting to summaries."
    ),
    BranchName.MARKET: (
        "Where the company sits in its market: customers and segments served, "
        "geographies, named competitors, partnerships, how it positions itself."
    ),
    BranchName.SIGNALS: (
        "What has changed recently: announcements, launches, expansion, acquisitions, "
        "hiring patterns, technology choices. Recent and dated, not history."
    ),
}


class BranchPlan(BaseModel):
    """One question to research in its own conversation."""

    name: BranchName
    question: str = Field(
        description=(
            "The specific question this branch must answer for this company, in one "
            "sentence, phrased for this company rather than generically."
        )
    )
    priority: int = Field(
        description=(
            "1 is most important. Branches are run in priority order, so if the run "
            "budget runs out the least important work is what gets dropped."
        )
    )
    rationale: str = Field(
        description="Why this branch is worth its budget for this company."
    )


class ResearchPlan(BaseModel):
    """The branches to run for one company."""

    branches: list[BranchPlan]
    reasoning: str = Field(
        description="One or two sentences on why this set, and what was left out."
    )


PLANNER_SYSTEM = """\
You are planning research into one company. You will not do the research yourself: each
branch you choose is handed to a separate agent with its own budget.

Choose the fewest branches that answer the objectives. Every branch costs real money and
runs whether or not it finds anything, so a branch that is unlikely to be relevant to
*this* company's objectives is waste, not thoroughness. Four well-chosen branches beat
six where two were filler.

Give each branch a question specific to this company, not a restatement of its area.
Order by priority: the lowest-priority branch is the first to be dropped if the run
budget runs out.

Pick only from the branch areas you are given, and do not repeat an area.
"""


def build_planner_prompt(
    *,
    domain: str,
    name: str | None,
    objectives: str | None,
    requested_fields: list[str],
    icp: dict | None,
    max_branches: int,
) -> str:
    lines = [
        f"Company: {name or domain} ({domain})",
        "",
        f"You may choose at most {max_branches} branches.",
        "",
        "## Available branch areas",
    ]
    for branch, brief in BRANCH_BRIEFS.items():
        lines.append(f"- **{branch.value}**: {brief}")
    lines.append("")

    if objectives:
        lines += ["## Research objectives", objectives.strip(), ""]
    else:
        lines += [
            "## Research objectives",
            "None given, so cover the requested fields as efficiently as possible.",
            "",
        ]

    lines += ["## Fields that must be populated"]
    lines += [f"- {f}" for f in requested_fields]
    lines.append("")

    if icp:
        lines.append("## Ideal customer profile to assess against")
        lines += [f"- {k}: {v}" for k, v in icp.items()]
        lines.append("")

    lines.append("Choose the branches.")
    return "\n".join(lines)


def plan_research(
    *,
    client: Anthropic,
    settings: Settings,
    domain: str,
    name: str | None,
    objectives: str | None,
    requested_fields: list[str],
    icp: dict | None = None,
) -> tuple[ResearchPlan, Any]:
    """Choose this company's branches. Returns the plan and the call's usage."""
    max_branches = settings.depth_profile.max_branches

    if max_branches <= 1:
        # Shallow runs are the pre-tree behaviour: one branch covering everything, and
        # no reason to pay a model to decide that.
        return (
            ResearchPlan(
                branches=[
                    BranchPlan(
                        name=BranchName.IDENTITY,
                        question=(
                            objectives.strip()
                            if objectives
                            else f"Establish the requested fields for {name or domain}."
                        ),
                        priority=1,
                        rationale="Shallow depth: one pass over everything.",
                    )
                ],
                reasoning="Shallow depth, so no planning call was made.",
            ),
            None,
        )

    response = client.messages.parse(
        model=settings.synthesis_model,
        max_tokens=4_000,
        system=[
            {
                "type": "text",
                "text": PLANNER_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": build_planner_prompt(
                    domain=domain,
                    name=name,
                    objectives=objectives,
                    requested_fields=requested_fields,
                    icp=icp,
                    max_branches=max_branches,
                ),
            }
        ],
        output_format=ResearchPlan,
    )
    plan = response.parsed_output
    if plan is None:
        raise RuntimeError(
            f"planner returned no parsed output (stop_reason={response.stop_reason})"
        )
    return (
        normalise_plan(plan, max_branches, label=name or domain),
        getattr(response, "usage", None),
    )


# Who runs a company and what the entity is are not optional areas of research --
# they are what a lead is. asked for "who runs it by name and role", the
# planner chose identity, products, financials and signals for one company and left
# people out, so a run that read the representative's name off the page it was
# already on recorded nobody. Asking is not enforcing.
REQUIRED_BRANCHES: tuple[BranchName, ...] = (BranchName.IDENTITY, BranchName.PEOPLE)

_REQUIRED_QUESTIONS = {
    BranchName.IDENTITY: (
        "What is the registered legal name, registration number, legal status and "
        "head office address of the company behind {label}?"
    ),
    BranchName.PEOPLE: (
        "Who runs {label}? Name every director, officer or senior manager the "
        "company or a public register publishes, with their role."
    ),
}


def normalise_plan(
    plan: ResearchPlan, max_branches: int, label: str = "this company"
) -> ResearchPlan:
    """Enforce the caps, and the floor, in code rather than trusting the prompt.

    The model is asked for at most ``max_branches`` distinct areas. Asking is not
    enforcing: a duplicated area would research the same pages twice and an extra
    branch would spend budget the depth setting did not authorise.

    It is also not enforcing in the other direction. Where there is room, identity
    and people are added if the planner left them out, because a profile without
    either is not a lead.
    """
    seen: set[BranchName] = set()
    kept: list[BranchPlan] = []
    for branch in sorted(plan.branches, key=lambda b: b.priority):
        name = BranchName(branch.name)
        if name in seen:
            continue
        seen.add(name)
        kept.append(branch)
        if len(kept) >= max_branches:
            break
    if not kept:
        raise ValueError("the planner returned no usable branches")

    # Put back what the planner dropped, by displacing the least important optional
    # area rather than spending more than the depth setting allows.
    for required in REQUIRED_BRANCHES:
        if required in seen:
            continue
        if len(kept) >= max_branches:
            droppable = [b for b in kept if BranchName(b.name) not in REQUIRED_BRANCHES]
            if not droppable:
                continue
            kept.remove(max(droppable, key=lambda b: b.priority))
        kept.append(
            BranchPlan(
                name=required.value,
                question=_REQUIRED_QUESTIONS[required].format(label=label),
                priority=min(9, max((b.priority for b in kept), default=1) + 1),
                rationale=(
                    "Added in code: a company profile without this is not a lead."
                ),
            )
        )
        seen.add(required)

    kept.sort(key=lambda b: b.priority)
    return ResearchPlan(branches=kept, reasoning=plan.reasoning)
