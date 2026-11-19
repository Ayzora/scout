"""The robots.txt check, and the switch that turns it off.

This file exists because of a real failure. Scout refused to open wmas.nhs.uk for a
whole run -- three branches, every one of them blocked on their first navigation --
and the window sat on about:blank while the findings all came from server-side
fetching. The site's robots.txt says ``Disallow:`` with nothing after it, which means
*allow everything*. What actually happened was that the firewall answered a
``Python-urllib`` user agent with 403, and the standard library reads a 403 on
robots.txt as "this host forbids everything".

So the tests below pin both halves: a robots.txt that cannot be read is not a
prohibition, and one that can be read is still obeyed.
"""

from __future__ import annotations

import http.server
import threading
from dataclasses import replace

import pytest

from scout.browser.session import BrowserSession, RobotsCache
from scout.config import load_settings


class _RobotsHandler(http.server.BaseHTTPRequestHandler):
    """Serves whatever robots.txt the test asks for, and counts the requests."""

    robots_status = 200
    robots_body = "User-agent: *\nDisallow:\n"
    hits: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server's spelling
        type(self).hits.append(self.path)
        if self.path == "/robots.txt":
            if self.robots_status != 200:
                self.send_error(self.robots_status)
                return
            body = self.robots_body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"<html><body><p>a page</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def robots_site():
    """A host whose robots.txt the test controls. Yields (url, configure)."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RobotsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def configure(status: int = 200, body: str = "User-agent: *\nDisallow:\n"):
        _RobotsHandler.robots_status = status
        _RobotsHandler.robots_body = body
        _RobotsHandler.hits = []

    configure()
    try:
        yield base, configure
    finally:
        server.shutdown()
        server.server_close()
        configure()


# --------------------------------------------------------------- reading the file


def test_an_empty_disallow_allows_everything(robots_site):
    """``Disallow:`` with no path is the permissive form, and real sites use it.

    This is wmas.nhs.uk's actual file.
    """
    base, configure = robots_site
    configure(body="User-agent: *\nDisallow:\nCrawl-delay: 10\n")
    allowed, why = RobotsCache().allowed(f"{base}/about")
    assert allowed, why


def test_a_real_disallow_is_obeyed(robots_site):
    base, configure = robots_site
    configure(body="User-agent: *\nDisallow: /private\n")
    cache = RobotsCache()
    assert cache.allowed(f"{base}/public")[0]
    allowed, why = cache.allowed(f"{base}/private/page")
    assert not allowed
    assert why == "disallowed by robots.txt"


def test_a_403_on_robots_txt_is_not_a_prohibition(robots_site):
    """The regression. A firewall refusing our fetch says nothing about crawl policy.

    RFC 9309 agrees: a 4xx means no rules are published, so there is nothing to obey.
    Before this fix, every page on such a host was refused.
    """
    base, configure = robots_site
    configure(status=403)
    allowed, why = RobotsCache().allowed(f"{base}/about")
    assert allowed, f"a 403 on robots.txt blocked the whole host: {why}"


@pytest.mark.parametrize("status", [401, 404, 500])
def test_an_unreadable_robots_txt_never_blocks(robots_site, status):
    base, configure = robots_site
    configure(status=status)
    assert RobotsCache().allowed(f"{base}/about")[0]


def test_robots_txt_is_fetched_as_the_browser_not_as_urllib(robots_site):
    """The cause of the 403: the default user agent is one firewalls reject."""
    base, configure = robots_site
    configure()
    RobotsCache().allowed(f"{base}/about")
    assert "Python-urllib" not in RobotsCache._UA
    assert "Mozilla/5.0" in RobotsCache._UA


def test_each_host_is_fetched_once(robots_site):
    base, configure = robots_site
    configure()
    cache = RobotsCache()
    for path in ("/a", "/b", "/c"):
        cache.allowed(f"{base}{path}")
    assert _RobotsHandler.hits.count("/robots.txt") == 1


# ------------------------------------------------------------------- the switch


def test_disabled_allows_what_robots_txt_forbids(robots_site):
    base, configure = robots_site
    configure(body="User-agent: *\nDisallow: /\n")
    assert not RobotsCache(enabled=True).allowed(f"{base}/anything")[0]
    assert RobotsCache(enabled=False).allowed(f"{base}/anything")[0]


def test_disabled_does_not_even_fetch_robots_txt(robots_site):
    base, configure = robots_site
    configure()
    RobotsCache(enabled=False).allowed(f"{base}/anything")
    assert "/robots.txt" not in _RobotsHandler.hits


def test_disabled_still_refuses_a_scheme_the_browser_cannot_open(robots_site):
    """Off means "skip the courtesy check", not "navigate to anything at all"."""
    allowed, why = RobotsCache(enabled=False).allowed("file:///etc/passwd")
    assert not allowed
    assert "scheme" in (why or "")


def test_the_session_passes_the_setting_through(tmp_path):
    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, respect_robots=False
    ) as session:
        assert session._robots.enabled is False
    with BrowserSession(screenshot_dir=tmp_path / "shots", headed=False) as session:
        assert session._robots.enabled is True, "the default must be to respect it"


def test_the_setting_defaults_to_respecting_robots(monkeypatch):
    """The repository default stays honest whatever one operator's .env says."""
    monkeypatch.delenv("SCOUT_RESPECT_ROBOTS", raising=False)
    assert load_settings().respect_robots is True
    monkeypatch.setenv("SCOUT_RESPECT_ROBOTS", "false")
    assert load_settings().respect_robots is False


def test_a_blocked_navigation_reports_why(robots_site, tmp_path):
    """End to end: the agent is told it was refused, and by what."""
    base, configure = robots_site
    configure(body="User-agent: *\nDisallow: /\n")
    settings = replace(load_settings(), respect_robots=True)
    assert settings.respect_robots
    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, respect_robots=True
    ) as session:
        result = session.goto(f"{base}/about")
    assert result.view.blocked
    assert "robots.txt" in result.view.text
