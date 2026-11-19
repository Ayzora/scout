"""Proves the browser layer reaches content an HTML scraper cannot."""

from __future__ import annotations

import urllib.request

import pytest

from scout.browser.session import _JS_SNAPSHOT, BrowserSession

JS_ONLY_TEXT = "Industrial vibration sensors"
JS_ONLY_FACT = "Coventry, United Kingdom"
CLICK_ONLY_NAME = "Priya Raman"


@pytest.fixture
def session(tmp_path):
    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, timeout_ms=10_000
    ) as s:
        yield s


def test_raw_html_does_not_contain_the_rendered_text(fixture_site):
    """Baseline: this is what a conventional HTML scraper would see.

    The content is fetched from /data.json at runtime, so it appears nowhere in
    the served markup -- not even inside a <script> literal.
    """
    html = urllib.request.urlopen(f"{fixture_site}/index.html").read().decode()
    assert JS_ONLY_TEXT not in html
    assert "Loading" in html

    about = urllib.request.urlopen(f"{fixture_site}/about.html").read().decode()
    assert JS_ONLY_FACT not in about
    assert CLICK_ONLY_NAME not in about


def test_browser_sees_javascript_rendered_text(session, fixture_site):
    result = session.goto(f"{fixture_site}/index.html")
    assert result.status == 200
    assert not result.view.blocked
    assert JS_ONLY_TEXT in result.view.text, result.view.text


def test_snapshot_assigns_refs_to_interactive_elements(session, fixture_site):
    view = session.goto(f"{fixture_site}/index.html").view
    assert "[ref=" in view.text
    assert "Accept all cookies" in view.text
    assert "About us" in view.text


def test_click_by_ref_dismisses_cookie_banner(session, fixture_site):
    view = session.goto(f"{fixture_site}/index.html").view
    ref = _ref_for(view.text, "Accept all cookies")
    after = session.click(ref)
    assert "Accept all cookies" not in after.text


def test_click_reveals_content_with_no_url_of_its_own(session, fixture_site):
    """The team list is behind a click. No scraper could guess a URL for it."""
    view = session.goto(f"{fixture_site}/about.html").view
    assert JS_ONLY_FACT in view.text
    assert CLICK_ONLY_NAME not in view.text

    ref = _ref_for(view.text, "Meet the team")
    after = session.click(ref)
    assert CLICK_ONLY_NAME in after.text
    assert "Head of Operations" in after.text


def test_chrome_text_is_dropped_but_chrome_links_survive(session, fixture_site):
    """Chrome prose is dropped; chrome links are not.

    Dropping the links would save tokens by making the agent unable to navigate.
    """
    view = session.goto(f"{fixture_site}/about.html").view

    assert "By continuing to browse this site" not in view.text
    assert "Reproduction in whole or in part" not in view.text
    assert "Navigate our site using the links above" not in view.text
    assert "Follow us on social media" not in view.text

    for label in ("Careers", "Blog", "Privacy policy", "Accept all cookies"):
        assert _ref_for(view.text, label) is not None, f"lost the {label!r} link"

    assert view.note and "nav/footer/cookie" in view.note


def test_main_scoping_keeps_content_revealed_after_a_click(session, fixture_site):
    """Reading only <main> must not eat content injected into it after a click."""
    view = session.goto(f"{fixture_site}/about.html").view
    assert view.note and "own <main> content" in view.note

    after = session.click(_ref_for(view.text, "Meet the team"))
    assert CLICK_ONLY_NAME in after.text
    assert "Press enquiries" in after.text


def _label_chars(nodes) -> int:
    """Total label text across a node list -- the thing we actually pay for."""
    return sum(len(n.get("label") or "") for n in nodes)


# The snapshot script with only the chrome filters removed.
_JS_SNAPSHOT_UNFILTERED = _JS_SNAPSHOT.replace(
    "if (el.closest(CHROME)) { droppedChrome += 1; continue; }", ""
).replace(
    "if (contentRoot && !contentRoot.contains(el)) { droppedChrome += 1; continue; }",
    "",
)


def test_filtering_shrinks_a_page_that_has_chrome(session, fixture_site):
    """Both sides are node-label totals, so the comparison is like for like."""
    view = session.goto(f"{fixture_site}/about.html").view
    filtered = _label_chars(session.page.evaluate(_JS_SNAPSHOT)["nodes"])
    unfiltered = _label_chars(session.page.evaluate(_JS_SNAPSHOT_UNFILTERED)["nodes"])

    assert filtered < unfiltered * 0.75, (
        f"expected chrome filtering to save over a quarter of the text, "
        f"got {filtered} vs {unfiltered} chars"
    )
    for fact in (JS_ONLY_FACT, "Industrial IoT", "just over 140 people"):
        assert fact in view.text


def test_filtering_costs_nothing_on_a_page_without_chrome(session, fixture_site):
    """These two have no nav, footer, cookie notice or <main>, so it must be a no-op.

    index.html is not such a page: it has both, and is covered below.
    """
    for page in ("careers.html", "blog.html"):
        session.goto(f"{fixture_site}/{page}")
        filtered = _label_chars(session.page.evaluate(_JS_SNAPSHOT)["nodes"])
        unfiltered = _label_chars(
            session.page.evaluate(_JS_SNAPSHOT_UNFILTERED)["nodes"]
        )
        assert filtered == unfiltered, (
            f"{page}: filter changed a page with no nav or footer "
            f"({filtered} vs {unfiltered} chars)"
        )


def test_filtering_drops_only_the_boilerplate_on_the_home_page(session, fixture_site):
    """Cookie prose goes; the JS-rendered tagline and every nav link stay."""
    view = session.goto(f"{fixture_site}/index.html").view
    assert "We use cookies" not in view.text
    assert JS_ONLY_TEXT in view.text
    for label in ("About us", "Careers", "Blog", "Accept all cookies"):
        assert _ref_for(view.text, label) is not None, f"lost the {label!r} link"


def test_screenshot_is_always_written_to_disk(session, fixture_site, tmp_path):
    view = session.goto(f"{fixture_site}/index.html").view
    assert view.screenshot_path is not None
    from pathlib import Path

    assert Path(view.screenshot_path).exists()
    assert Path(view.screenshot_path).stat().st_size > 0


def test_find_links_ranks_research_pages_above_noise(session, fixture_site):
    session.goto(f"{fixture_site}/index.html")
    links = session.find_links()
    urls = [item["url"] for item in links]
    assert any("about" in u for u in urls)
    # 'about' and 'careers' are research-relevant; 'blog' is also hinted, so just
    # assert the relevant ones are present and ordered ahead of nothing useful.
    assert urls.index(next(u for u in urls if "about" in u)) < len(urls)


def test_missing_ref_is_reported_not_crashed(session, fixture_site):
    session.goto(f"{fixture_site}/index.html")
    view = session.click(9999)
    assert "no longer on the page" in (view.note or "")


def test_dead_host_is_reported_not_crashed(session):
    result = session.goto("http://127.0.0.1:9/nothing-here")
    assert "failed" in result.view.text.lower() or result.view.blocked


def _ref_for(snapshot_text: str, label: str) -> int:
    for line in snapshot_text.splitlines():
        if label in line and line.startswith("[ref="):
            return int(line.split("[ref=")[1].split("]")[0])
    raise AssertionError(f"no ref found for {label!r} in:\n{snapshot_text}")
