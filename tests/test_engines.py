"""The browser engine versus the fetch engine.

`web_fetch` runs on Anthropic's servers, so these tests cover the boundary rather than
the fetch itself: which tools each engine gets, what each is told, and -- with a plain
HTTP read standing in for a server-side fetch -- what a non-rendering reader misses.
"""

from __future__ import annotations

import urllib.request
from dataclasses import replace

import pytest

from scout.agent.prompts import system_prompt
from scout.agent.recorder import Recorder
from scout.agent.tools import SERVER_TOOLS, build_tools
from scout.browser.session import BrowserSession
from scout.config import VALID_ENGINES, load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope

JS_ONLY_FACT = "Coventry, United Kingdom"
CLICK_ONLY_NAME = "Priya Raman"

RECORDING_TOOLS = {
    "record_findings",
    "mark_unknown",
    "record_person",
    "finish_research",
}
# Reached over plain HTTP, so every engine has them. Keeping them common is what
# keeps the engine comparison about page access alone.
SHARED_TOOLS = {"lookup_registry"}

BROWSER_TOOLS = {
    "open_page",
    "browser_goto",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_back",
    "browser_find_links",
}


def _settings(tmp_path, engine: str):
    return replace(
        load_settings(), data_dir=tmp_path, engine=engine, headed=False, max_steps=50
    )


def _recorder(settings):
    reset_engine()
    init_db(settings)
    with session_scope(settings) as s:
        company = m.Company(domain="northwind.example")
        run = m.Run(model="test", effort="high", engine=settings.engine)
        s.add_all([company, run])
        s.flush()
        company_run = m.CompanyRun(run_id=run.id, company_id=company.id)
        s.add(company_run)
        s.flush()
        return Recorder(settings=settings, company_run_id=company_run.id)


# ------------------------------------------------------------------ tool surface


def test_fetch_engine_gets_no_browser_tools(tmp_path):
    settings = _settings(tmp_path, "fetch")
    names = {
        t.name
        for t in build_tools(
            session=None, recorder=_recorder(settings), settings=settings
        )
    }
    assert names == RECORDING_TOOLS | SHARED_TOOLS
    assert not (names & BROWSER_TOOLS)


def test_every_engine_can_reach_a_register(tmp_path):
    """A register is plain HTTP. Withholding it from one engine would make the
    comparison a test of that rather than of how pages are read."""
    for engine in VALID_ENGINES:
        settings = _settings(tmp_path / f"reg-{engine}", engine)
        session = None
        if engine in ("browser", "browser_toolset"):
            session = BrowserSession(
                screenshot_dir=tmp_path / f"reg-{engine}" / "shots", headed=False
            )
            session.start()
        try:
            names = {
                t.name
                for t in build_tools(
                    session=session, recorder=_recorder(settings), settings=settings
                )
                if hasattr(t, "name")
            }
        finally:
            if session is not None:
                session.close()
        assert SHARED_TOOLS <= names, engine


def test_browser_engine_gets_both(tmp_path):
    settings = _settings(tmp_path, "browser")
    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, timeout_ms=10_000
    ) as browser:
        names = {
            t.name
            for t in build_tools(
                session=browser, recorder=_recorder(settings), settings=settings
            )
        }
    assert BROWSER_TOOLS <= names
    assert RECORDING_TOOLS <= names


def test_both_engines_record_identically(tmp_path):
    """The recording surface must not differ, or the comparison is not like-for-like."""
    surfaces = {}
    for engine in VALID_ENGINES:
        settings = _settings(tmp_path / engine, engine)
        session = None
        if engine in ("browser", "browser_toolset"):
            session = BrowserSession(
                screenshot_dir=tmp_path / engine / "shots", headed=False
            )
            session.start()
        try:
            tools = build_tools(
                session=session, recorder=_recorder(settings), settings=settings
            )
        finally:
            if session is not None:
                session.close()
        surfaces[engine] = {
            t.name: t.to_dict()["input_schema"]
            for t in tools
            # A toolset is one entry with many members and no single name, so it is
            # not part of the recording surface being compared.
            if getattr(t, "name", None) in RECORDING_TOOLS
        }
    assert surfaces["browser"] == surfaces["fetch"]


def test_browser_engine_refuses_to_run_without_a_session(tmp_path):
    settings = _settings(tmp_path, "browser")
    with pytest.raises(ValueError, match="needs a BrowserSession"):
        build_tools(session=None, recorder=_recorder(settings), settings=settings)


def test_fetch_engine_never_launches_a_browser(tmp_path):
    """Launching Chromium for an engine with no browser tools would be pure waste."""
    from scout.agent.loop import _browser_for

    with _browser_for(_settings(tmp_path, "fetch"), tmp_path / "shots") as browser:
        assert browser is None
    # And nothing was written where screenshots would go.
    assert not (tmp_path / "shots").exists()

    with _browser_for(_settings(tmp_path, "browser"), tmp_path / "shots") as browser:
        assert browser is not None
        assert browser.page is not None


def test_two_branches_do_not_collide_on_step_numbers(tmp_path):
    """The first bug branching caused, and it failed quietly.

    ``steps.seq`` is unique per company run, but every branch gets its own Recorder.
    Counters starting from zero in each collided the moment a second branch acted, and
    because the agent simply retried through other tools the run still reported
    success while losing findings.
    """
    settings = _settings(tmp_path, "browser")
    reset_engine()
    init_db(settings)
    with session_scope(settings) as s:
        company = m.Company(domain="northwind.example")
        run = m.Run(model="test", effort="medium", engine="browser")
        s.add_all([company, run])
        s.flush()
        company_run = m.CompanyRun(run_id=run.id, company_id=company.id)
        s.add(company_run)
        s.flush()
        company_run_id = company_run.id

    # Two branches, two recorders, same company run -- exactly the live arrangement.
    first = Recorder(settings=settings, company_run_id=company_run_id, branch_id=None)
    second = Recorder(settings=settings, company_run_id=company_run_id, branch_id=None)
    for _ in range(3):
        first.log_step(step_type="message", tool_name="a")
    for _ in range(3):
        second.log_step(step_type="message", tool_name="b")
    # A third recorder joining later must also continue the sequence.
    third = Recorder(settings=settings, company_run_id=company_run_id, branch_id=None)
    third.log_step(step_type="message", tool_name="c")

    with session_scope(settings) as s:
        seqs = [
            row.seq
            for row in s.query(m.Step)
            .filter(m.Step.company_run_id == company_run_id)
            .order_by(m.Step.seq)
            .all()
        ]
    assert seqs == [1, 2, 3, 4, 5, 6, 7], seqs


def test_server_tools_are_capped(tmp_path):
    """An uncapped web_fetch would make the fetch engine look expensive unfairly."""
    fetch_tool = next(t for t in SERVER_TOOLS if t["name"] == "web_fetch")
    assert fetch_tool["max_content_tokens"] > 0
    assert fetch_tool["max_uses"] > 0


# ------------------------------------------------------------------ scope defaults


def test_default_fields_are_small():
    """The default drives how much the agent chases, so it is part of the cost."""
    from scout.agent.prompts import DEFAULT_FIELDS, FULL_PROFILE_FIELDS, resolve_fields

    assert len(DEFAULT_FIELDS) < len(FULL_PROFILE_FIELDS)
    assert resolve_fields(None) == DEFAULT_FIELDS
    assert resolve_fields("full") == FULL_PROFILE_FIELDS
    assert resolve_fields("FULL") == FULL_PROFILE_FIELDS
    assert resolve_fields("industry, ceo_name") == ["industry", "ceo_name"]
    # Everything the core set names must survive into the full set.
    assert set(DEFAULT_FIELDS) <= set(FULL_PROFILE_FIELDS)
    with pytest.raises(ValueError, match="no field names"):
        resolve_fields(",,,")


# ------------------------------------------------------------------ what each is told


def test_each_engine_is_told_only_what_it_has():
    browser = system_prompt("browser")
    fetch = system_prompt("fetch")

    assert "browser_click" in browser
    assert "browser_click" not in fetch
    assert "You have no browser" in fetch

    # The rules that make findings comparable are shared, word for word.
    for shared in ("# Recording what you find", "# People", "# Scope", "# Finishing"):
        assert shared in browser and shared in fetch

    # The fetch engine is told to report its blind spots rather than paper over them.
    assert "mark_unknown" in fetch
    assert "JavaScript" in fetch


def test_every_engine_is_told_how_to_corroborate():
    """Corroboration was the point of the tree and it measured 2% on the first run.

    The rule is shared, so no engine is held to a weaker evidence standard, and it
    has to name the source hierarchy or "independent" means nothing.
    """
    for engine in VALID_ENGINES:
        prompt = system_prompt(engine)
        assert "# Corroborating what matters" in prompt
        assert "two pages of the same site is not" in prompt
        # The hierarchy, or the agent cannot tell a filing from a directory entry.
        assert "Statutory registers" in prompt
        assert "openly editable" in prompt
        # And a reminder that it is a flag, not a gate.
        assert "single-sourced, not discarded" in prompt
        # Without a cost caveat the agent would corroborate everything.
        assert "Do not spend your whole budget corroborating" in prompt


# ------------------------------------- the hypothesis the evaluation set must settle


def test_a_non_rendering_reader_cannot_see_what_the_browser_sees(
    fixture_site, tmp_path
):
    """The project's premise, stated as a test so it can be proved wrong.

    The fixture's facts arrive via ``fetch('/data.json')`` and its leadership list
    exists only after a click. If this ever fails, the README should say so.
    """
    served = urllib.request.urlopen(f"{fixture_site}/about.html").read().decode()
    assert JS_ONLY_FACT not in served
    assert CLICK_ONLY_NAME not in served

    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, timeout_ms=10_000
    ) as browser:
        view = browser.goto(f"{fixture_site}/about.html").view
        assert JS_ONLY_FACT in view.text

        ref = next(
            int(line.split("[ref=")[1].split("]")[0])
            for line in view.text.splitlines()
            if "Meet the team" in line and "[ref=" in line
        )
        assert CLICK_ONLY_NAME in browser.click(ref).text
