"""The planner: branch choice, caps, and the shallow shortcut.

The planner is a new failure point — a bad branch list wastes a whole run — so the
parts that do not need the API are tested here: the prompt it is given, the caps
enforced on what it returns, and the shallow path that makes no call at all.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from scout.agent.plan import (
    BRANCH_BRIEFS,
    BranchName,
    BranchPlan,
    ResearchPlan,
    build_planner_prompt,
    normalise_plan,
    plan_research,
)
from scout.config import DEPTH_PROFILES, load_settings


def _branch(name, priority=1):
    return BranchPlan(
        name=name, question=f"q for {name}", priority=priority, rationale="because"
    )


# ------------------------------------------------------------------- the taxonomy


def test_every_branch_area_has_a_brief():
    """A branch the planner can choose but cannot be briefed on would run blind."""
    assert set(BRANCH_BRIEFS) == set(BranchName)
    for brief in BRANCH_BRIEFS.values():
        assert len(brief) > 40


def test_people_brief_carries_the_data_protection_rules():
    """The constraint has to travel with the branch that researches people."""
    brief = BRANCH_BRIEFS[BranchName.PEOPLE].lower()
    assert "no email guessing" in brief
    assert "contact database" in brief


# ----------------------------------------------------------------------- the caps


def test_more_branches_than_allowed_are_dropped_lowest_priority_first():
    plan = ResearchPlan(
        branches=[
            _branch(BranchName.SIGNALS, priority=5),
            _branch(BranchName.IDENTITY, priority=1),
            _branch(BranchName.PEOPLE, priority=2),
            _branch(BranchName.MARKET, priority=4),
        ],
        reasoning="r",
    )
    kept = normalise_plan(plan, max_branches=2)
    assert [b.name for b in kept.branches] == [BranchName.IDENTITY, BranchName.PEOPLE]


def test_a_repeated_area_is_collapsed():
    """Two branches on one area would research the same pages twice."""
    plan = ResearchPlan(
        branches=[
            _branch(BranchName.PRODUCTS, priority=1),
            _branch(BranchName.PRODUCTS, priority=2),
            _branch(BranchName.PEOPLE, priority=3),
        ],
        reasoning="r",
    )
    kept = normalise_plan(plan, max_branches=6)
    names = [b.name for b in kept.branches]
    assert names.count(BranchName.PRODUCTS) == 1
    assert BranchName.PEOPLE in names
    # Identity is filled in because there was room; see the required-branch tests.
    assert BranchName.IDENTITY in names


def test_an_empty_plan_is_an_error_not_an_empty_run():
    with pytest.raises(ValueError, match="no usable branches"):
        normalise_plan(ResearchPlan(branches=[], reasoning="r"), max_branches=4)


def test_caps_are_enforced_in_code_not_only_asked_for_in_the_prompt():
    """The prompt states the limit; this is what makes it true."""
    plan = ResearchPlan(
        branches=[_branch(n, priority=i) for i, n in enumerate(BranchName, start=1)],
        reasoning="r",
    )
    assert len(plan.branches) == 6
    assert len(normalise_plan(plan, max_branches=4).branches) == 4


# -------------------------------------------------------------------- the prompt


def test_prompt_contains_the_company_objectives_fields_and_limit():
    prompt = build_planner_prompt(
        domain="monzo.com",
        name="Monzo",
        objectives="What do they sell, and who leads the company?",
        requested_fields=["company_name", "industry"],
        icp={"sector": "fintech"},
        max_branches=4,
    )
    assert "monzo.com" in prompt and "Monzo" in prompt
    assert "who leads the company" in prompt
    assert "- company_name" in prompt and "- industry" in prompt
    assert "at most 4 branches" in prompt
    assert "sector: fintech" in prompt
    # Every area must be offered, or the planner cannot choose it.
    for branch in BranchName:
        assert f"**{branch.value}**" in prompt


def test_prompt_handles_no_objectives_without_leaving_a_hole():
    prompt = build_planner_prompt(
        domain="x.example",
        name=None,
        objectives=None,
        requested_fields=["company_name"],
        icp=None,
        max_branches=4,
    )
    assert "None given" in prompt
    assert "x.example" in prompt


# ------------------------------------------------------------ the shallow shortcut


def test_shallow_depth_makes_no_api_call():
    """Paying a model to decide there is one branch would be absurd.

    A client that raises on use proves no call happens.
    """

    class Exploding:
        @property
        def messages(self):
            raise AssertionError("shallow depth must not call the API")

    settings = replace(load_settings(), depth="shallow")
    plan, usage = plan_research(
        client=Exploding(),
        settings=settings,
        domain="monzo.com",
        name="Monzo",
        objectives="What do they sell?",
        requested_fields=["company_name"],
    )
    assert len(plan.branches) == 1
    assert usage is None
    assert plan.branches[0].question == "What do they sell?"


def test_depth_profiles_grow_monotonically():
    shallow, standard, deep = (
        DEPTH_PROFILES["shallow"],
        DEPTH_PROFILES["standard"],
        DEPTH_PROFILES["deep"],
    )
    assert shallow.max_branches < standard.max_branches < deep.max_branches
    assert shallow.max_children == standard.max_children == 0
    assert deep.max_children > 0
    # Shallow must stay the pre-tree behaviour: one pass, no children.
    assert shallow.max_branches == 1


def test_run_budget_scales_with_depth():
    """The per-branch budget is multiplied by how many branches depth allows, so a
    deep run has a stated ceiling rather than an open-ended one."""
    base = replace(load_settings(), task_budget_tokens=40_000)
    assert replace(base, depth="shallow").run_budget_tokens == 40_000
    assert replace(base, depth="standard").run_budget_tokens == 160_000
    assert replace(base, depth="deep").run_budget_tokens == 400_000


# ------------------------------------------------- what a plan must always contain


def _plan(*names, priorities=None):
    from scout.agent.plan import BranchPlan, ResearchPlan

    priorities = priorities or list(range(1, len(names) + 1))
    return ResearchPlan(
        branches=[
            BranchPlan(name=n, question=f"about {n}?", priority=p, rationale="x")
            for n, p in zip(names, priorities)
        ],
        reasoning="test",
    )


def test_people_is_added_when_the_planner_leaves_it_out():
    """Measured: asked "who runs it by name and role", the planner chose identity,
    products, financials and signals, so a run that had the representative's name on
    a page it already read recorded nobody."""
    from scout.agent.plan import normalise_plan

    out = normalise_plan(
        _plan("identity", "products", "financials", "signals"), max_branches=4,
        label="Stay Factory",
    )
    names = [b.name for b in out.branches]
    assert "people" in names
    assert "identity" in names
    assert len(out.branches) == 4, "the cap still holds"


def test_the_displaced_branch_is_the_least_important_optional_one():
    from scout.agent.plan import normalise_plan

    out = normalise_plan(
        _plan("identity", "products", "financials", "signals",
              priorities=[1, 2, 3, 4]),
        max_branches=4, label="Acme",
    )
    names = [b.name for b in out.branches]
    assert "signals" not in names, "the lowest-priority optional area goes first"
    assert "products" in names and "financials" in names


def test_a_required_branch_that_is_added_carries_a_real_question():
    from scout.agent.plan import normalise_plan

    out = normalise_plan(_plan("products"), max_branches=4, label="Stay Factory")
    people = next(b for b in out.branches if b.name == "people")
    assert "Stay Factory" in people.question
    assert "role" in people.question
    assert people.rationale


def test_a_plan_that_already_has_both_is_left_alone():
    from scout.agent.plan import normalise_plan

    plan = _plan("identity", "people", "products")
    out = normalise_plan(plan, max_branches=4, label="Acme")
    assert [b.name for b in out.branches] == ["identity", "people", "products"]


def test_room_is_used_before_anything_is_displaced():
    from scout.agent.plan import normalise_plan

    out = normalise_plan(_plan("products", "signals"), max_branches=4, label="Acme")
    names = [b.name for b in out.branches]
    assert set(names) == {"products", "signals", "identity", "people"}


def test_the_cap_is_never_exceeded():
    from scout.agent.plan import normalise_plan

    for cap in (2, 3, 4, 6):
        out = normalise_plan(
            _plan("products", "financials", "signals", "market"),
            max_branches=cap, label="Acme",
        )
        assert len(out.branches) <= cap, cap


def test_a_two_branch_cap_keeps_both_required_areas():
    from scout.agent.plan import normalise_plan

    out = normalise_plan(_plan("products", "signals"), max_branches=2, label="Acme")
    assert {b.name for b in out.branches} == {"identity", "people"}


def test_shallow_depth_still_makes_no_planning_call(tmp_path):
    """One branch cannot hold both required areas, and shallow is the pre-tree
    behaviour on purpose."""
    from dataclasses import replace

    from scout.agent.plan import plan_research
    from scout.config import load_settings

    settings = replace(load_settings(), depth="shallow", data_dir=tmp_path)
    plan, usage = plan_research(
        client=None, settings=settings, domain="acme.com", name=None,
        objectives="who runs it", requested_fields=["legal_name"],
    )
    assert usage is None
    assert len(plan.branches) == 1
