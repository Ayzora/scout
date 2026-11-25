"""What one branch learns, the next one is told.

Branches are separate conversations, which is what makes depth affordable. The price
is that each starts ignorant and searches for the company again. On one measured run that
was ten searches at a cent each -- 82% of the bill, against two cents for every token
of browsing and reasoning in the whole run.

These tests pin the handoff: the facts and URLs cross between branches, the later
branches are given a smaller search allowance, and the dossier stays small enough that
it does not trade a search charge for a context charge.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from scout.agent.branches import Dossier, build_branch_brief, read_dossier
from scout.agent.plan import BranchName, BranchPlan
from scout.agent.tools import (
    WEB_SEARCH_MAX_USES,
    WEB_SEARCH_MAX_USES_WITH_DOSSIER,
    server_tools,
)
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope


@pytest.fixture
def company_run(tmp_path):
    settings = replace(load_settings(), data_dir=tmp_path)
    reset_engine()
    init_db(settings)
    with session_scope(settings) as s:
        company = m.Company(domain="trust.example.nhs.uk")
        run = m.Run(model="test", effort="medium", engine="browser")
        s.add_all([company, run])
        s.flush()
        cr = m.CompanyRun(run_id=run.id, company_id=company.id)
        s.add(cr)
        s.flush()
        cr_id = cr.id
    return settings, cr_id


def _record(settings, cr_id, field, value, url, kind=m.FindingKind.FACT.value):
    with session_scope(settings) as s:
        f = m.Finding(company_run_id=cr_id, field=field, value=value, kind=kind)
        s.add(f)
        s.flush()
        s.add(m.Evidence(finding_id=f.id, source_url=url, quote="q"))


# ------------------------------------------------------------- reading it back


def test_an_empty_run_has_an_empty_dossier(company_run):
    settings, cr_id = company_run
    assert read_dossier(settings, cr_id).is_empty()


def test_facts_and_urls_cross_from_one_branch_to_the_next(company_run):
    settings, cr_id = company_run
    _record(settings, cr_id, "company_name", '"Example University NHS Foundation Trust"',
            "https://www.cqc.org.uk/provider/RYA/contact")
    _record(settings, cr_id, "headquarters_address", '"Brierley Hill DY5 1LX"',
            "https://www.cqc.org.uk/provider/RYA/contact")

    dossier = read_dossier(settings, cr_id)
    fields = dict(dossier.facts)
    assert fields["company_name"].startswith("Example University")
    assert "DY5 1LX" in fields["headquarters_address"]
    # One URL, cited twice, is one page to go back to.
    assert dossier.urls == ["https://www.cqc.org.uk/provider/RYA/contact"]


def test_only_one_value_per_field_is_carried(company_run):
    """The point is to stop a branch re-establishing a field, not to replay it."""
    settings, cr_id = company_run
    for i in range(5):
        _record(settings, cr_id, "products_services", f'"service {i}"',
                f"https://example.com/{i}")
    dossier = read_dossier(settings, cr_id)
    assert [f for f, _ in dossier.facts] == ["products_services"]
    assert len(dossier.urls) == 5


def test_inferences_and_unknowns_are_not_presented_as_established(company_run):
    settings, cr_id = company_run
    _record(settings, cr_id, "industry", '"guessed"', "https://example.com/a",
            kind=m.FindingKind.INFERENCE.value)
    with session_scope(settings) as s:
        s.add(m.Finding(company_run_id=cr_id, field="revenue", value="unknown",
                        kind=m.FindingKind.UNKNOWN.value))
    assert read_dossier(settings, cr_id).facts == []


def test_the_dossier_is_capped(company_run):
    """It enters every later branch's context, so it cannot grow with the run."""
    settings, cr_id = company_run
    for i in range(40):
        _record(settings, cr_id, f"field_{i}", '"' + "x" * 500 + '"',
                f"https://example.com/{i}")
    dossier = read_dossier(settings, cr_id)
    assert len(dossier.facts) <= 12
    assert len(dossier.urls) <= 10
    assert all(len(v) <= 120 for _, v in dossier.facts)


def test_branch_summaries_are_carried(company_run):
    settings, cr_id = company_run
    _record(settings, cr_id, "company_name", '"Example Trust"', "https://example.com/a")
    with session_scope(settings) as s:
        s.add(m.Branch(company_run_id=cr_id, name="identity", depth=1,
                       status=m.Status.DONE.value, summary="The legal name is the "
                       "University variant of the Trust."))
    notes = read_dossier(settings, cr_id).notes
    assert notes and notes[0].startswith("identity:")


def test_an_unfinished_branch_contributes_no_conclusion(company_run):
    settings, cr_id = company_run
    _record(settings, cr_id, "company_name", '"Example Trust"', "https://example.com/a")
    with session_scope(settings) as s:
        s.add(m.Branch(company_run_id=cr_id, name="products", depth=1,
                       status=m.Status.RUNNING.value, summary="half done"))
    assert read_dossier(settings, cr_id).notes == []


# ------------------------------------------------------------ reaching the brief


def _brief(dossier):
    return build_branch_brief(
        domain="trust.example.nhs.uk",
        name=None,
        branch=BranchPlan(name=BranchName.PRODUCTS.value, question="What do they sell?",
                          priority=1, rationale="the ask"),
        requested_fields=["products_services"],
        icp=None,
        allow_followups=False,
        dossier=dossier,
    )


def test_the_brief_tells_a_branch_not_to_search_for_what_is_known():
    brief = _brief(Dossier(
        facts=[("company_name", "Example University NHS Foundation Trust")],
        urls=["https://www.cqc.org.uk/provider/RYA/contact"],
        notes=["identity: the University variant"],
    ))
    assert "Do not spend a search establishing these again" in brief
    assert "Example University NHS Foundation Trust" in brief
    assert "cqc.org.uk" in brief
    assert "rather than searching to find it again" in brief


def test_the_first_branch_gets_a_brief_with_no_handoff_section():
    assert "Already established" not in _brief(Dossier())
    assert "Already established" not in _brief(None)


# ----------------------------------------------------------- the search allowance


def test_a_briefed_branch_is_given_fewer_searches():
    assert WEB_SEARCH_MAX_USES_WITH_DOSSIER < WEB_SEARCH_MAX_USES
    first = server_tools(WEB_SEARCH_MAX_USES)
    later = server_tools(WEB_SEARCH_MAX_USES_WITH_DOSSIER)
    assert first[0]["name"] == "web_search"
    assert first[0]["max_uses"] == WEB_SEARCH_MAX_USES
    assert later[0]["max_uses"] == WEB_SEARCH_MAX_USES_WITH_DOSSIER


def test_the_allowance_never_reaches_zero():
    """Zero would not be frugal, it would be a branch that cannot find anything."""
    assert server_tools(0)[0]["max_uses"] >= 1


def test_changing_one_branch_allowance_does_not_change_the_next():
    """The constant is shared, so the tools have to be copies rather than aliases."""
    from scout.agent.tools import SERVER_TOOLS

    server_tools(1)[0]["max_uses"] = 99
    assert SERVER_TOOLS[0]["max_uses"] == WEB_SEARCH_MAX_USES
    assert server_tools()[0]["max_uses"] == WEB_SEARCH_MAX_USES


def test_the_handoff_does_not_suppress_a_second_source():
    """A measured regression, caused by this feature.

    The first wording said "take these as given, do not search for them again", and
    corroboration went from one claim to none: the duplicate work it removed was
    exactly the work that produced a second source. The brief now has to stop the
    agent *searching* again without stopping it *recording* a confirmation it meets.
    """
    brief = _brief(Dossier(facts=[("company_name", "Example University NHS Trust")]))
    assert "Do not spend a search establishing these again" in brief
    assert "different" in brief and "record it as a finding" in brief
    assert "Do not go looking for one" in brief
