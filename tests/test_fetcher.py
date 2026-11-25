"""Fetch first, and let the page say whether that was enough.

The rule this file pins is per page, not per host. Measured on real sites, raw HTTP
text against browser-rendered text: Acme's homepage gives 479 characters out of
239KB of HTML, while Companies House, the CQC and a Greenhouse job board all come back
complete. The registries worth corroborating against serve clean HTML; the company
marketing sites are the JavaScript shells. A host rule would get both wrong.

The fixture site is the controlled case: it renders its content from /data.json, so
plain HTTP cannot see it and must escalate.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from scout.browser.fetcher import (
    MIN_TEXT_CHARS,
    MIN_TEXT_RATIO,
    FetchResult,
    html_to_text,
    http_get,
    looks_like_a_shell,
    needs_browser,
)


def _result(text: str = "", html_bytes: int = 0, status: int | None = 200, error=None):
    return FetchResult(
        url="https://example.com", status=status, text=text,
        html_bytes=html_bytes, error=error,
    )


# ------------------------------------------------------------------ text extraction


def test_script_and_style_contents_are_not_mistaken_for_text():
    """A shell is mostly script. Counting it as text would hide exactly what we look for."""
    html = (
        "<html><head><style>body{color:red}</style>"
        "<script>var x = 'a very long string of javascript '.repeat(100)</script>"
        "</head><body><p>Hello world</p></body></html>"
    )
    assert html_to_text(html) == "Hello world"


def test_entities_and_whitespace_are_folded():
    assert html_to_text("<p>Acme&nbsp;&amp;   Sons</p>") == "Acme & Sons"


def test_a_shell_container_is_recognised():
    assert looks_like_a_shell('<body><div id="root"></div></body>')
    assert looks_like_a_shell('<div id="__next">')
    assert not looks_like_a_shell("<body><p>real content</p></body>")


# ------------------------------------------------------------------ the decision


def test_a_readable_page_is_not_escalated():
    escalate, why = needs_browser(_result(text="x " * 2_000, html_bytes=20_000))
    assert not escalate
    assert "readable" in why


def test_a_javascript_shell_is_escalated():
    """Acme's homepage, to scale: a few hundred characters out of a huge document."""
    escalate, why = needs_browser(_result(text="a" * 479, html_bytes=239_227))
    assert escalate
    assert "479" in why and "JavaScript" in why


def test_a_large_page_that_is_mostly_script_is_escalated():
    """Enough text to pass the floor, but not enough to be the page."""
    text = "word " * 400            # 2,000 chars, above MIN_TEXT_CHARS
    escalate, why = needs_browser(_result(text=text, html_bytes=900_000))
    assert len(text) > MIN_TEXT_CHARS, "this case must not be caught by the floor"
    assert escalate
    assert "script" in why


def test_the_two_thresholds_do_not_overlap():
    """A page at the floor with ordinary markup must pass, or the floor is the rule."""
    text = "x" * (MIN_TEXT_CHARS + 1)
    html_bytes = int(len(text) / (MIN_TEXT_RATIO * 2))
    assert not needs_browser(_result(text=text, html_bytes=html_bytes))[0]


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_a_refusal_escalates_rather_than_being_reported_as_the_page(status):
    escalate, why = needs_browser(
        _result(text="", status=status, error=f"HTTP {status}")
    )
    assert escalate
    assert str(status) in why


def test_a_network_failure_escalates():
    escalate, why = needs_browser(_result(error="URLError: no route"))
    assert escalate
    assert "failed" in why


def test_a_challenge_page_escalates_even_when_it_is_long():
    body = "Just a moment... " + ("please wait while we check your browser " * 100)
    escalate, why = needs_browser(_result(text=body, html_bytes=10_000))
    assert escalate
    assert "challenge" in why


# ------------------------------------------------------------------ over the wire


def test_fetching_the_fixture_site_sees_only_the_shell(fixture_site):
    """The controlled case: content comes from /data.json, so HTTP cannot see it."""
    result = http_get(f"{fixture_site}/index.html")
    assert result.ok
    assert "Industrial vibration sensors" not in result.text
    assert needs_browser(result)[0], "a JS-only page must escalate"


def test_a_server_error_is_captured_not_raised():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_error(503)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = http_get(f"http://127.0.0.1:{server.server_port}/x")
        assert not result.ok
        assert result.status == 503
        assert needs_browser(result)[0]
    finally:
        server.shutdown()
        server.server_close()


def test_an_unresolvable_host_returns_an_error_rather_than_raising():
    result = http_get("https://this-host-does-not-exist.invalid/")
    assert result.error is not None
    assert needs_browser(result)[0]


def test_a_thinly_populated_app_container_is_escalated():
    """Server-rendered fragment plus a React root: most of the page is still to come."""
    from scout.browser.fetcher import MARGINAL_TEXT_CHARS

    # Ratio deliberately healthy, so only the shell rule can catch this.
    text = "x" * (MIN_TEXT_CHARS + 50)
    result = _result(text=text, html_bytes=len(text) * 10)
    result.is_shell = True
    assert len(text) < MARGINAL_TEXT_CHARS
    escalate, why = needs_browser(result)
    assert escalate
    assert "still to render" in why


def test_a_full_page_is_not_escalated_merely_for_being_an_app():
    """Plenty of sites render fully and still ship a root container."""
    from scout.browser.fetcher import MARGINAL_TEXT_CHARS

    text = "word " * MARGINAL_TEXT_CHARS
    result = _result(text=text, html_bytes=len(text) * 3)
    result.is_shell = True
    assert not needs_browser(result)[0]


def test_the_shell_flag_is_set_by_fetching(fixture_site):
    result = http_get(f"{fixture_site}/static.html")
    assert result.ok and not result.is_shell
