"""Drives the agent's tools by hand, the way the model would, with no API key.

This exercises the whole data path -- browser, tool, recorder, database, provenance
guard -- so a failure here is a real bug rather than a model behaviour.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from scout.agent.recorder import Recorder
from scout.agent.tools import build_tools
from scout.browser.session import BrowserSession
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope


@pytest.fixture
def ctx(tmp_path):
    """A settings object, initialised DB, browser session and bound tools."""
    settings = replace(load_settings(), data_dir=tmp_path, max_steps=50, headed=False)
    reset_engine()
    init_db(settings)

    with session_scope(settings) as s:
        company = m.Company(domain="northwind.example", name="Northwind Sensors")
        run = m.Run(model="test", effort="high")
        s.add_all([company, run])
        s.flush()
        company_run = m.CompanyRun(run_id=run.id, company_id=company.id)
        s.add(company_run)
        s.flush()
        company_run_id = company_run.id

    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, timeout_ms=10_000
    ) as browser:
        recorder = Recorder(settings=settings, company_run_id=company_run_id)
        tools = {t.name: t for t in build_tools(
            session=browser, recorder=recorder, settings=settings
        )}
        yield settings, recorder, tools, company_run_id
    reset_engine()


def _call(tools, _tool, /, **kwargs):
    """Invoke a tool the way the runner does. ``_tool`` is positional-only so it
    cannot collide with a tool argument also called ``name``."""
    return tools[_tool].call(kwargs)


def test_goto_returns_rendered_text_and_logs_a_step(ctx, fixture_site):
    settings, recorder, tools, cr_id = ctx
    out = _call(tools, "browser_goto", url=f"{fixture_site}/index.html")
    assert "Industrial vibration sensors" in out
    assert "[ref=" in out

    with session_scope(settings) as s:
        steps = s.query(m.Step).filter(m.Step.company_run_id == cr_id).all()
    assert len(steps) == 1
    assert steps[0].tool_name == "browser_goto"
    assert steps[0].screenshot_path and Path(steps[0].screenshot_path).exists()


def test_screenshot_reaches_model_only_when_requested(ctx, fixture_site):
    settings, recorder, tools, cr_id = ctx
    text_only = _call(tools, "browser_goto", url=f"{fixture_site}/index.html")
    assert isinstance(text_only, str)

    with_image = _call(tools, "browser_snapshot", include_screenshot=True)
    assert isinstance(with_image, list)
    kinds = [b["type"] for b in with_image]
    assert kinds == ["text", "image"]
    assert with_image[1]["source"]["media_type"] == "image/png"
    assert len(with_image[1]["source"]["data"]) > 1000

    # ...but a screenshot was still written to disk for the dashboard both times.
    with session_scope(settings) as s:
        paths = [
            st.screenshot_path
            for st in s.query(m.Step).filter(m.Step.company_run_id == cr_id).all()
        ]
    assert all(p and Path(p).exists() for p in paths)


def test_finding_is_stored_with_its_evidence(ctx, fixture_site):
    settings, recorder, tools, cr_id = ctx
    _call(tools, "browser_goto", url=f"{fixture_site}/about.html")
    msg = _call(
        tools,
        "record_finding",
        field="headquarters_location",
        value="Coventry, United Kingdom",
        kind="fact",
        evidence_url=f"{fixture_site}/about.html",
        evidence_quote="Headquarters: Coventry, United Kingdom",
    )
    assert "Recorded fact" in msg
    assert "WARNING" not in msg

    with session_scope(settings) as s:
        finding = s.query(m.Finding).filter(m.Finding.company_run_id == cr_id).one()
        assert finding.kind == "fact"
        assert finding.value == "Coventry, United Kingdom"
        ev = finding.evidence
        assert len(ev) == 1
        assert ev[0].source_url.endswith("/about.html")
        assert "Coventry" in ev[0].quote
        # Evidence for the open page carries the screenshot the agent was looking at.
        assert ev[0].screenshot_path and Path(ev[0].screenshot_path).exists()


def test_invented_source_url_is_flagged(ctx, fixture_site):
    """The main failure mode for evidence-backed extraction, caught."""
    settings, recorder, tools, cr_id = ctx
    _call(tools, "browser_goto", url=f"{fixture_site}/index.html")
    msg = _call(
        tools,
        "record_finding",
        field="industry",
        value="Aerospace",
        kind="fact",
        evidence_url="https://totally-different-site.example/about",
        evidence_quote="We are an aerospace company",
    )
    assert "WARNING" in msg
    assert "not a page this session opened" in msg
    assert recorder.unsourced_claims == 1


def test_same_host_other_page_is_not_flagged(ctx, fixture_site):
    """A page on a host we did visit is plausible and should not cry wolf."""
    settings, recorder, tools, cr_id = ctx
    _call(tools, "browser_goto", url=f"{fixture_site}/index.html")
    msg = _call(
        tools,
        "record_finding",
        field="careers",
        value="Hiring a CTO",
        kind="fact",
        evidence_url=f"{fixture_site}/careers.html",
        evidence_quote="We are hiring a Chief Technology Officer.",
    )
    assert "WARNING" not in msg


def test_unknown_kind_is_rejected_by_the_schema(ctx):
    """``kind`` is an enum of fact|inference, so the SDK rejects anything else
    before our code runs. The runner turns that into an error tool_result the
    model can see and correct."""
    settings, recorder, tools, cr_id = ctx
    with pytest.raises(ValueError, match="Invalid arguments"):
        _call(
            tools,
            "record_finding",
            field="industry",
            value="unclear",
            kind="unknown",
            evidence_url="https://x.example",
            evidence_quote="q",
        )
    with session_scope(settings) as s:
        assert s.query(m.Finding).count() == 0


def test_recorder_also_rejects_unknown_kind_directly(ctx):
    """Defence in depth: the persistence layer refuses it too, so a future caller
    that bypasses the tool schema cannot write an unlabelled claim."""
    settings, recorder, tools, cr_id = ctx
    with pytest.raises(ValueError, match="mark_unknown"):
        recorder.record_finding(
            field_name="industry",
            value="unclear",
            kind="unknown",
            evidence_url="https://x.example",
            evidence_quote="q",
        )


def test_mark_unknown_records_a_gap_explicitly(ctx):
    settings, recorder, tools, cr_id = ctx
    _call(tools, "mark_unknown", field="revenue", reason="not published anywhere")
    with session_scope(settings) as s:
        finding = s.query(m.Finding).one()
    assert finding.kind == "unknown"
    assert finding.value is None
    assert "not published" in finding.reason


def test_person_requires_published_source_and_stores_contact(ctx, fixture_site):
    settings, recorder, tools, cr_id = ctx
    _call(tools, "browser_goto", url=f"{fixture_site}/about.html")
    _call(
        tools,
        "record_person",
        name="Priya Raman",
        role="Chief Executive Officer",
        seniority="executive",
        email="press@northwind-sensors.example",
        evidence_url=f"{fixture_site}/about.html",
        evidence_quote="Priya Raman — Chief Executive Officer",
    )
    with session_scope(settings) as s:
        person = s.query(m.Person).one()
        assert person.name == "Priya Raman"
        assert person.contact == {"email": "press@northwind-sensors.example"}
        assert len(person.evidence) == 1


def test_step_limit_stops_the_agent(ctx, fixture_site):
    settings, recorder, tools, cr_id = ctx
    recorder._seq = settings.max_steps  # pretend we have used the budget
    out = _call(tools, "browser_goto", url=f"{fixture_site}/index.html")
    assert "Step limit" in out
    assert "finish_research" in out


def test_stats_report_evidence_coverage(ctx, fixture_site):
    settings, recorder, tools, cr_id = ctx
    _call(tools, "browser_goto", url=f"{fixture_site}/about.html")
    _call(
        tools, "record_finding", field="industry", value="Industrial IoT",
        kind="fact", evidence_url=f"{fixture_site}/about.html",
        evidence_quote="Industry: Industrial IoT hardware manufacturing",
    )
    _call(
        tools, "record_finding", field="expansion", value="Growing in Leeds",
        kind="inference", evidence_url=f"{fixture_site}/about.html",
        evidence_quote="Active markets: United Kingdom, Germany and Poland",
        reason="markets list implies expansion",
    )
    _call(tools, "mark_unknown", field="revenue", reason="not published")

    stats = recorder.stats()
    assert stats["facts"] == 1
    assert stats["inferences"] == 1
    assert stats["unknowns"] == 1
    assert stats["evidence_coverage"] == 1.0


def test_finish_research_closes_the_run(ctx):
    settings, recorder, tools, cr_id = ctx
    out = _call(tools, "finish_research", summary="Found the basics.")
    assert recorder.finished is True
    assert recorder.final_summary == "Found the basics."
    assert "Do not call any more tools" in out


def test_click_reveals_people_then_records_them(ctx, fixture_site):
    """The end-to-end shape of the thing: interact, observe, record with provenance."""
    settings, recorder, tools, cr_id = ctx
    view = _call(tools, "browser_goto", url=f"{fixture_site}/about.html")
    assert "Priya Raman" not in view

    ref = None
    for line in view.splitlines():
        if "Meet the team" in line and line.startswith("[ref="):
            ref = int(line.split("[ref=")[1].split("]")[0])
    assert ref is not None

    after = _call(tools, "browser_click", ref=ref)
    assert "Priya Raman" in after
    assert "Head of Operations" in after

    _call(
        tools,
        "record_person",
        name="Tomasz Nowak",
        role="Head of Operations",
        evidence_url=f"{fixture_site}/about.html",
        evidence_quote="Tomasz Nowak — Head of Operations",
    )
    with session_scope(settings) as s:
        assert s.query(m.Person).count() == 1
