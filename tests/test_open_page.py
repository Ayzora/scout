"""`open_page`: the agent asks for a page, the code decides how to get it.

The escalation is deliberately not the model's call. It cannot see a status code or a
text-to-HTML ratio, so asking it to choose would be asking it to guess -- and the
answer drifts with the web rather than with the prompt. Code decides how to *get* the
page; the model still decides whether the page answered its question.

The fixture site is the controlled case, serving its content only via /data.json.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from scout.agent.recorder import Recorder
from scout.agent.tools import build_tools
from scout.browser.session import BrowserSession
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope


@pytest.fixture
def ctx(tmp_path):
    settings = replace(
        load_settings(), data_dir=tmp_path, headed=False, engine="browser",
        respect_robots=False,
    )
    reset_engine()
    init_db(settings)
    with session_scope(settings) as s:
        company = m.Company(domain="northwind.example")
        run = m.Run(model="test", effort="medium", engine="browser")
        s.add_all([company, run])
        s.flush()
        cr = m.CompanyRun(run_id=run.id, company_id=company.id)
        s.add(cr)
        s.flush()
        cr_id = cr.id

    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, timeout_ms=10_000,
        respect_robots=settings.respect_robots,
    ) as browser:
        recorder = Recorder(
            settings=settings, company_run_id=cr_id, company_domain="northwind.example"
        )
        tools = {t.name: t for t in build_tools(
            session=browser, recorder=recorder, settings=settings
        )}
        yield settings, recorder, tools, cr_id
    reset_engine()


def _steps(settings, cr_id):
    with session_scope(settings) as s:
        return (
            s.query(m.Step)
            .filter(m.Step.company_run_id == cr_id)
            .order_by(m.Step.seq)
            .all()
        )


def test_a_javascript_page_is_escalated_and_the_content_arrives(ctx, fixture_site):
    """The whole point. Plain HTTP sees a shell; the reader still gets the page."""
    settings, _, tools, cr_id = ctx
    out = tools["open_page"].call({"url": f"{fixture_site}/index.html"})
    assert "Opened in the browser because" in out
    assert "Industrial vibration sensors" in out, "escalation must return the content"


def test_escalation_says_why(ctx, fixture_site):
    _, _, tools, _ = ctx
    out = tools["open_page"].call({"url": f"{fixture_site}/index.html"})
    assert "JavaScript" in out or "characters of text" in out


def test_escalation_leaves_the_browser_on_that_page(ctx, fixture_site):
    """So a ref in the result can be clicked without navigating again."""
    _, _, tools, _ = ctx
    out = tools["open_page"].call({"url": f"{fixture_site}/index.html"})
    assert "[ref=" in out
    snapshot = tools["browser_snapshot"].call({})
    assert "Industrial vibration sensors" in snapshot


def test_a_static_page_is_read_directly_without_the_browser(ctx, fixture_site):
    settings, _, tools, cr_id = ctx
    out = tools["open_page"].call({"url": f"{fixture_site}/static.html"})
    assert "Read directly" in out
    assert "Opened in the browser" not in out


def test_both_paths_record_a_step_with_the_url(ctx, fixture_site):
    settings, _, tools, cr_id = ctx
    tools["open_page"].call({"url": f"{fixture_site}/static.html"})
    tools["open_page"].call({"url": f"{fixture_site}/index.html"})
    steps = [s for s in _steps(settings, cr_id) if s.tool_name == "open_page"]
    assert len(steps) == 2
    assert all(s.url for s in steps)
    # The escalation is recorded, so the rate can be measured rather than guessed.
    assert steps[1].tool_input.get("escalated") is True
    assert steps[1].tool_input.get("reason")


def test_a_directly_read_page_counts_as_visited_for_provenance(ctx, fixture_site):
    """A claim cited to a page we actually read must not be flagged as unsourced."""
    _, recorder, tools, _ = ctx
    url = f"{fixture_site}/static.html"
    tools["open_page"].call({"url": url})
    assert url in recorder.visited_urls or url in recorder.fetched_urls


def test_a_bare_domain_is_accepted(ctx, fixture_site):
    _, _, tools, _ = ctx
    out = tools["open_page"].call({"url": "this-host-does-not-exist.invalid"})
    # Normalised to https:// and attempted; what matters is it did not raise.
    assert "invalid" in out


def test_an_unreachable_page_reports_rather_than_raising(ctx):
    _, _, tools, _ = ctx
    out = tools["open_page"].call({"url": "https://this-host-does-not-exist.invalid/"})
    assert isinstance(out, str) and out


def test_robots_is_honoured_when_the_setting_is_on(tmp_path, fixture_site):
    """The fetch path has to obey the same rule the browser path does."""
    settings = replace(
        load_settings(), data_dir=tmp_path, headed=False, engine="browser",
        respect_robots=True,
    )
    reset_engine()
    init_db(settings)
    with session_scope(settings) as s:
        company = m.Company(domain="northwind.example")
        run = m.Run(model="test", effort="medium", engine="browser")
        s.add_all([company, run])
        s.flush()
        cr = m.CompanyRun(run_id=run.id, company_id=company.id)
        s.add(cr)
        s.flush()
        cr_id = cr.id

    class _DenyAll:
        def can_fetch(self, *_args) -> bool:
            return False

    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, respect_robots=True
    ) as browser:
        host = fixture_site[: fixture_site.index("/", 8)] if "/" in fixture_site[8:] else fixture_site
        browser._robots._cache[host] = _DenyAll()
        recorder = Recorder(settings=settings, company_run_id=cr_id)
        tools = {t.name: t for t in build_tools(
            session=browser, recorder=recorder, settings=settings
        )}
        out = tools["open_page"].call({"url": f"{fixture_site}/static.html"})
    assert "refused" in out.lower()
    assert "robots.txt" in out
    reset_engine()


def test_a_long_page_is_capped(ctx, fixture_site):
    """A directly read page enters context as text and needs the snapshot's ceiling."""
    from scout.agent.tools import _FETCH_TEXT_CAP

    _, _, tools, _ = ctx
    out = tools["open_page"].call({"url": f"{fixture_site}/long.html"})
    assert len(out) < _FETCH_TEXT_CAP + 1_000
