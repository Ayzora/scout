"""The browser_toolset engine: Scout's browser behind Anthropic's member names.

Driven directly against the fixture site, with no API key. What is being checked is
the translation layer -- Anthropic's `ref_N` references and member signatures onto
Scout's own numbered refs and session -- plus the contract with the SDK, which decides
which members are offered to the model at all.
"""

from __future__ import annotations

import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scout.agent.recorder import Recorder
from scout.agent.tools import build_tools
from scout.browser.session import BrowserSession
from scout.browser.toolset import (
    ScoutBrowserToolset,
    _ref_number,
    render_page,
)
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope

JS_ONLY_FACT = "Coventry, United Kingdom"
CLICK_ONLY_NAME = "Priya Raman"

# Members Scout can express. Everything else must be disabled by the SDK rather than
# offered and then refused.
EXPECTED_MEMBERS = {
    "navigate",
    "read_page",
    "get_page_text",
    "find",
    "left_click",
    "form_input",
    "scroll",
    "screenshot",
}


@pytest.fixture
def toolset(tmp_path):
    settings = replace(
        load_settings(), data_dir=tmp_path, engine="browser_toolset", headed=False
    )
    reset_engine()
    init_db(settings)
    with session_scope(settings) as s:
        company = m.Company(domain="northwind.example")
        run = m.Run(model="test", effort="medium", engine="browser_toolset")
        s.add_all([company, run])
        s.flush()
        company_run = m.CompanyRun(run_id=run.id, company_id=company.id)
        s.add(company_run)
        s.flush()
        company_run_id = company_run.id

    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, timeout_ms=10_000
    ) as browser:
        recorder = Recorder(
            settings=settings,
            company_run_id=company_run_id,
            company_domain="northwind.example",
        )
        yield ScoutBrowserToolset(session=browser, recorder=recorder), settings, recorder


def _refs(text: str) -> dict[str, str]:
    """{label: ref_N} from a read_page rendering."""
    found = {}
    for label, ref in re.findall(r'"([^"]*)"\s+\[(ref_\d+)\]', text):
        found[label] = ref
    return found


# ------------------------------------------------------------ the SDK contract


def test_only_the_members_scout_can_express_are_offered(toolset):
    """The SDK disables what the subclass does not implement.

    That is what makes 31 members cost nothing: the model is never offered one that
    would have to be refused.
    """
    ts, _, _ = toolset
    definition = ts.to_dict()
    assert definition["type"] == "browser_toolset_20260801"

    configs = definition.get("configs") or {}
    disabled = {k for k, v in configs.items() if v.get("enabled") is False}
    assert disabled, "expected the SDK to disable unimplemented members"
    assert not (disabled & EXPECTED_MEMBERS), (
        f"a member Scout implements was disabled: {disabled & EXPECTED_MEMBERS}"
    )
    # Specifically: the dangerous and the unexpressible ones.
    for member in ("javascript_exec", "file_upload", "type", "left_click_drag"):
        assert member in disabled, f"{member} should not be offered"


def test_the_toolset_definition_is_cached(toolset):
    """It is part of the stable prefix, like the hand-written tool list it replaces."""
    ts, _, _ = toolset
    assert ts.to_dict().get("cache_control") == {"type": "ephemeral"}


def test_the_engine_supplies_the_toolset_plus_the_recording_tools(toolset):
    ts, settings, recorder = toolset
    tools = build_tools(session=ts.session, recorder=recorder, settings=settings)
    names = {getattr(t, "name", None) for t in tools}
    assert {"record_findings", "mark_unknown", "finish_research"} <= names
    # No hand-written browser tools, or the model would have two ways to do everything.
    assert not any(str(n).startswith("browser_") for n in names if n)
    assert any(isinstance(t, ScoutBrowserToolset) for t in tools)


# ------------------------------------------------------------ reference handling


def test_reference_numbers_are_translated_both_ways():
    assert _ref_number("ref_12") == 12
    assert _ref_number("ref_1") == 1
    with pytest.raises(ValueError, match="element reference"):
        _ref_number("not-a-ref")


def test_render_page_uses_the_toolsets_convention():
    """Scout renders `[ref=4] <a> Careers -> /x`; the toolset expects role first."""
    view = SimpleNamespace(
        url="https://x.example/",
        title="X",
        text="[ref=4] <a> Careers -> /careers.html\n<h1> Welcome\n[ref=5] <button> Go",
        blocked=False,
        note=None,
    )
    out = render_page(view)
    assert 'a "Careers" [ref_4] -> /careers.html' in out
    assert 'button "Go" [ref_5]' in out
    assert "h1: Welcome" in out
    assert "https://x.example/" in out


def test_render_page_reports_a_block_without_pretending_to_have_content():
    view = SimpleNamespace(
        url="https://x.example/", title="", text="", blocked=True, note="HTTP 403"
    )
    out = render_page(view)
    assert "BLOCKED" in out
    assert "Do not try to work around it" in out


# ----------------------------------------------------- driving the real browser


def test_navigate_then_read_page_sees_javascript_rendered_content(toolset, fixture_site):
    ts, _, _ = toolset
    result = ts.navigate(None, SimpleNamespace(url=f"{fixture_site}/about.html"))
    assert result.url.endswith("/about.html")
    assert result.status == 200

    page = ts.read_page(None, SimpleNamespace())
    # The premise of the project, through the other interface.
    assert JS_ONLY_FACT in page
    assert "ref_" in page


def test_clicking_a_ref_reaches_content_with_no_url(toolset, fixture_site):
    ts, _, _ = toolset
    ts.navigate(None, SimpleNamespace(url=f"{fixture_site}/about.html"))
    page = ts.read_page(None, SimpleNamespace())

    refs = _refs(page)
    target = next(r for label, r in refs.items() if "Meet the team" in label)
    ts.left_click(None, SimpleNamespace(target=SimpleNamespace(ref=target)))

    after = ts.read_page(None, SimpleNamespace())
    assert CLICK_ONLY_NAME in after


def test_clicking_by_coordinate_is_refused_with_a_usable_message(toolset, fixture_site):
    """Coordinates are what this design exists to avoid, so the refusal has to say
    what to do instead rather than just fail."""
    ts, _, _ = toolset
    ts.navigate(None, SimpleNamespace(url=f"{fixture_site}/index.html"))
    with pytest.raises(ValueError, match="read_page"):
        ts.left_click(None, SimpleNamespace(target=SimpleNamespace(x=10, y=10)))


def test_get_page_text_returns_what_read_page_filtered_out(toolset, fixture_site):
    """read_page drops nav and footer prose; get_page_text is the way back to it."""
    ts, _, _ = toolset
    ts.navigate(None, SimpleNamespace(url=f"{fixture_site}/about.html"))
    page = ts.read_page(None, SimpleNamespace())
    full = ts.get_page_text(None, SimpleNamespace())

    assert "Reproduction in whole or in part" not in page
    assert "Reproduction in whole or in part" in full
    assert JS_ONLY_FACT in full


def test_find_surfaces_candidate_pages(toolset, fixture_site):
    ts, _, _ = toolset
    ts.navigate(None, SimpleNamespace(url=f"{fixture_site}/index.html"))
    out = ts.find(None, SimpleNamespace(query="careers"))
    assert "careers" in out.lower()


def test_robots_is_enforced_before_the_driver_is_touched(toolset):
    """The URL policy refuses first, so a disallowed page never loads at all."""
    ts, _, _ = toolset
    ts.session._robots._cache["https://blocked.example"] = _DenyAll()
    with pytest.raises(ValueError, match="Respect it"):
        ts._check_url(None, "https://blocked.example/secret")


class _DenyAll:
    def can_fetch(self, *_args) -> bool:
        return False


# ------------------------------------------------- shared bookkeeping, per engine


def test_steps_and_page_context_are_recorded_identically(toolset, fixture_site):
    """The engines must differ in how they read pages and in nothing else.

    If this engine skipped the step trail, the screenshot or the page context the
    provenance guard reads, the eval would be comparing bookkeeping rather than
    browsers.
    """
    ts, settings, recorder = toolset
    ts.navigate(None, SimpleNamespace(url=f"{fixture_site}/about.html"))
    ts.read_page(None, SimpleNamespace())

    assert recorder.page.url.endswith("/about.html")
    assert recorder.page.screenshot_path
    assert any(u.endswith("/about.html") for u in recorder.visited_urls)

    with session_scope(settings) as s:
        steps = s.query(m.Step).order_by(m.Step.seq).all()
        names = [st.tool_name for st in steps]
        assert "browser.navigate" in names
        assert "browser.read_page" in names
        # Every step carries its screenshot, as the dashboard timeline needs.
        assert all(st.screenshot_path for st in steps if st.url)


def test_browser_state_always_reports_one_active_tab(toolset, fixture_site):
    """The SDK calls this after every member call, including refused ones, and the
    API requires exactly one active tab whenever any is open."""
    ts, _, _ = toolset
    before = ts._browser_state(None)
    assert len(before.tabs) == 1
    assert before.tabs[0]["active"] is True

    ts.navigate(None, SimpleNamespace(url=f"{fixture_site}/index.html"))
    after = ts._browser_state(None)
    assert after.tabs[0]["url"].endswith("/index.html")
    assert after.tabs[0]["active"] is True
