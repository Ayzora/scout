"""The dashboard, loaded in a real browser.

The page is plain HTML and JavaScript with no build step, so nothing type-checks it.
These tests are the compiler: they serve a database, open the page in Chromium, and
fail on a console error or on a view that did not render. Without them a typo in a
template string would only be found by looking.
"""

from __future__ import annotations

import os
import socket
import threading
from dataclasses import replace

import pytest
import uvicorn

from scout.api.app import create_app
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A real server over a small, finished run.

    The key is cleared for the whole module. Without that the page behaves one way
    on a machine that has one and another way in CI, and the suite passes or fails
    depending on whose shell it ran in.
    """
    tmp = tmp_path_factory.mktemp("ui")
    previous = os.environ.pop("ANTHROPIC_API_KEY", None)
    settings = replace(load_settings(), data_dir=tmp)
    reset_engine()
    init_db(settings)

    with session_scope(settings) as s:
        company = m.Company(domain="example.co")
        run = m.Run(label="demo", model="claude-haiku-5-5", effort="medium",
                    engine="browser", status=m.Status.DONE.value,
                    objectives="who runs it and what it sells",
                    icp={"sector": "UK mobility"})
        s.add_all([company, run])
        s.flush()
        cr = m.CompanyRun(run_id=run.id, company_id=company.id,
                          status=m.Status.DONE.value, cost_usd=0.0369,
                          duration_s=187.0, summary="Example Co runs UK bike share.")
        s.add(cr)
        s.flush()
        branch = m.Branch(company_run_id=cr.id, name="identity", depth=1,
                          status=m.Status.DONE.value, question="Legal entity?",
                          cost_usd=0.01, web_searches=0, summary="EXAMPLE HOLDINGS LTD.")
        s.add(branch)
        s.flush()
        for field, value, url in (
            ("legal_name", '"EXAMPLE HOLDINGS LTD."',
             "https://find-and-update.company-information.service.gov.uk/company/1"),
            # Recorded the way a real run records it, caveat and all.
            ("trading_name", '"Example Co (site header reads EXAMPLE.CO)"',
             "https://example.co/"),
            ("contact_email", '"hello@example.co"', "https://example.co/contact"),
            ("board_page_date", '"reviewed 2026"', "https://example.co/about"),
        ):
            f = m.Finding(company_run_id=cr.id, field=field, value=value,
                          kind="fact", branch_id=branch.id)
            s.add(f)
            s.flush()
            s.add(m.Evidence(finding_id=f.id, source_url=url,
                             quote="a sentence from the page"))
        person = m.Person(company_run_id=cr.id, name="Jordan Blake", role="Director")
        s.add(person)
        s.flush()
        s.add(m.Evidence(person_id=person.id, source_url="https://example.co/team",
                         quote="Jordan Blake"))
        s.add(m.Qualification(company_run_id=cr.id, score=72.0, tier="strong",
                              rationale="UK mobility operator.",
                              icp_match={"matched": ["sector"], "missed": []}))
        s.add(m.Step(company_run_id=cr.id, seq=1, type="tool_result",
                     tool_name="open_page", tool_input={"url": "https://example.co/"},
                     url="https://example.co/", duration_ms=120))
        s.add(m.Step(company_run_id=cr.id, seq=2, type="tool_result",
                     tool_name="open_page",
                     tool_input={"url": "https://example.co/app", "escalated": True,
                                 "reason": "only 300 characters of text"},
                     duration_ms=1900))
        ids = (run.id, cr.id)

    port = _free_port()
    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port,
                            log_level="error")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv_server.started:
            break
        threading.Event().wait(0.05)
    yield f"http://127.0.0.1:{port}", ids
    uv_server.should_exit = True
    thread.join(timeout=5)
    if previous is not None:
        os.environ["ANTHROPIC_API_KEY"] = previous
    reset_engine()


@pytest.fixture(scope="module")
def page(server):
    from playwright.sync_api import sync_playwright

    base, ids = server
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        problems: list[str] = []
        pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        pg.on("console", lambda msg: problems.append(f"console: {msg.text}")
              if msg.type == "error" else None)
        pg.problems = problems
        pg.base = base
        pg.ids = ids
        yield pg
        browser.close()


def _visit(page, path):
    page.goto(f"{page.base}/#{path}")
    page.wait_for_timeout(700)
    assert not page.problems, page.problems


def test_the_run_list_renders(page):
    _visit(page, "/")
    body = page.inner_text("#view")
    assert "demo" in body
    assert "who runs it" in body


def test_a_run_lists_its_companies(page):
    _, (run_id, _) = page.base, page.ids
    _visit(page, f"/runs/{run_id}")
    body = page.inner_text("#view")
    assert "example.co" in body
    assert "UK mobility" in body, "the ideal customer should be shown"


def test_the_company_is_titled_by_name_not_domain(page):
    """A domain is how you reached a company, not what it is called."""
    _, cr_id = page.ids
    _visit(page, f"/companies/{cr_id}")
    assert page.inner_text("h1").strip() == "Example Co"
    assert "example.co" in page.inner_text(".page-head")


def test_the_profile_shows_fixed_columns_and_the_verdict(page):
    _, cr_id = page.ids
    _visit(page, f"/companies/{cr_id}")
    body = page.inner_text("#view")
    assert "legal_name" in body and "EXAMPLE HOLDINGS LTD." in body
    assert "contact_email" in body, "contact details belong in the profile"
    assert "not established" in body, "an empty core column must say so"
    # The tier is upper-cased by the stylesheet, so compare case-insensitively.
    assert "strong" in body.lower() and "72" in body
    # A finding outside the vocabulary is shown, not dropped.
    assert "board_page_date" in body


def test_the_tabs_switch_without_reloading(page):
    _, cr_id = page.ids
    _visit(page, f"/companies/{cr_id}")
    page.click("button[data-tab=people]")
    page.wait_for_timeout(300)
    assert "Jordan Blake" in page.inner_text("#panel")
    page.click("button[data-tab=evidence]")
    page.wait_for_timeout(300)
    assert "a sentence from the page" in page.inner_text("#panel")
    page.click("button[data-tab=trace]")
    page.wait_for_timeout(600)
    trace = page.inner_text("#panel")
    assert "needed a real browser" in trace
    assert "only 300 characters" in trace
    assert not page.problems, page.problems


def test_the_form_cannot_be_started_without_a_key(page):
    """Nothing that costs money should be one click away when it cannot work."""
    _visit(page, "/new")
    assert page.get_attribute("#run-form button", "disabled") is not None
    assert "ANTHROPIC_API_KEY" in page.inner_text("#view")


def test_the_new_run_form_refuses_bad_json_before_spending(page):
    _visit(page, "/new")
    page.fill("#domains", "example.com")
    page.fill("#objectives", "who runs it")
    page.fill("#icp", "{not json")
    # Submitted rather than clicked: the validation has to hold whatever state the
    # Start button is in.
    page.dispatch_event("#run-form", "submit")
    page.wait_for_timeout(300)
    assert "valid JSON" in page.inner_text("#form-error")


def test_an_unknown_page_does_not_break_the_shell(page):
    _visit(page, "/nowhere")
    assert "No such page" in page.inner_text("#view")
    assert page.inner_text(".wordmark").strip().startswith("Scout")


def test_a_row_can_be_followed_from_the_keyboard(page):
    """The whole row is the target, so it has to behave like a link without a mouse."""
    _visit(page, "/")
    page.focus(".row-link")
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    assert "/runs/" in page.url
    assert not page.problems, page.problems


def test_the_tabs_are_announced_as_tabs(page):
    _, cr_id = page.ids
    _visit(page, f"/companies/{cr_id}")
    assert page.get_attribute("#tabs", "role") == "tablist"
    assert page.get_attribute("button[data-tab=profile]", "aria-selected") == "true"
    assert page.get_attribute("button[data-tab=people]", "aria-selected") == "false"


def test_it_reads_on_a_phone(page):
    """One column, no horizontal scrolling, nothing clipped."""
    _, cr_id = page.ids
    page.set_viewport_size({"width": 390, "height": 844})
    try:
        _visit(page, f"/companies/{cr_id}")
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        if overflow > 1:
            # Name what is sticking out, so the failure is actionable.
            wide = page.evaluate("""() => {
              const w = window.innerWidth;
              return [...document.querySelectorAll('*')]
                .filter((el) => el.getBoundingClientRect().right > w + 1)
                .map((el) => `${el.tagName}.${el.className} "${
                  (el.textContent || '').trim().slice(0, 40)}"`)
                .slice(0, 6);
            }""")
            raise AssertionError(f"{overflow}px of horizontal scroll: {wide}")
        assert "EXAMPLE HOLDINGS LTD." in page.inner_text("#view")
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


def test_a_caveat_in_the_name_does_not_become_the_heading(page):
    """A recorded value often carries a parenthetical note. It belongs in the row,
    not in the heading, where it pushes the name off the line."""
    _, cr_id = page.ids
    _visit(page, f"/companies/{cr_id}")
    assert page.inner_text("h1").strip() == "Example Co"
    # The full value, caveat included, is still shown where it belongs.
    assert "site header reads EXAMPLE.CO" in page.inner_text("#panel")


@pytest.mark.parametrize("width", [390, 1024, 1280, 1440, 1728])
def test_the_column_is_centred_at_every_width(page, width):
    """A max-width with no auto margin pins the column to the left and dumps the
    whole surplus on the right. At 1728px that was 612px of nothing on one side and
    a 36px gutter on the other, which is what it looked like."""
    _, cr_id = page.ids
    page.set_viewport_size({"width": width, "height": 900})
    try:
        _visit(page, f"/companies/{cr_id}")
        gaps = page.evaluate(
            """() => {
              const right = [...document.querySelectorAll('.profile-aside')]
                .map((e) => e.getBoundingClientRect().right);
              const left = [...document.querySelectorAll('.profile-field')]
                .map((e) => e.getBoundingClientRect().left);
              return {
                left: Math.round(Math.min(...left)),
                right: Math.round(window.innerWidth - Math.max(...right)),
                wordmark: Math.round(
                  document.querySelector('.wordmark').getBoundingClientRect().left),
                overflow: Math.round(
                  document.documentElement.scrollWidth - window.innerWidth),
              };
            }"""
        )
        assert abs(gaps["left"] - gaps["right"]) <= 2, gaps
        assert gaps["left"] >= 16, f"gutter too tight: {gaps}"
        # The masthead shares the column, so the wordmark sits over the first field.
        assert abs(gaps["wordmark"] - gaps["left"]) <= 2, gaps
        assert gaps["overflow"] <= 1, gaps
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})
