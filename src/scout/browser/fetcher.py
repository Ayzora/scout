"""Read a page over plain HTTP, and judge whether that was enough.

The browser is the only thing that works on a site built as a JavaScript shell, and it
is dead weight on a page that serves its content as HTML. Which of those a page is
cannot be predicted from its host -- it has to be observed, and observing it is cheap.

Measured on real pages, raw HTTP text against browser-rendered text:

    monzo.com                      479 chars of   239,227  ratio 0.05 of rendered
    oxwash.com                     refused the request outright
    wmas.nhs.uk                  4,584 chars              ratio 0.67
    Companies House officers     9,827 chars              ratio 1.03
    CQC provider page            4,985 chars              ratio 1.97
    Greenhouse job board         4,180 chars              ratio 1.03

The split is not where it looks. Registries, regulators and job boards -- the sources
worth corroborating against -- serve clean HTML. The company marketing sites are the
JavaScript shells. So the rule is per page, not per host, and the page itself says
which it is: 479 characters out of 239KB of HTML is not a page anyone could read.

This module makes that judgement and nothing else. It does not decide policy; the
caller decides what to do with ``escalate``.
"""

from __future__ import annotations

import gzip
import re
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass

# What the browser would send. A plain client identifier is answered with 403 by a
# large share of sites, which says nothing about whether we are welcome to read them.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Below this a page has not told us anything, whatever its HTML says. Monzo's homepage
# yields 479 characters; the thinnest page here that is genuinely readable yields
# several thousand.
MIN_TEXT_CHARS = 900

# Text as a share of raw HTML. Catches the large page that is nearly all script: the
# real pages above sit between 0.03 and 0.07, the shell at 0.002.
MIN_TEXT_RATIO = 0.015

# A page that both carries a single-page-app container and is only thinly populated is
# most likely server-rendering a fragment and filling in the rest. Escalating costs a
# few seconds; missing the body of the page costs the finding.
MARGINAL_TEXT_CHARS = MIN_TEXT_CHARS * 3

# Beyond this there is no point reading further for a judgement about the page.
MAX_BYTES = 3_000_000

_SCRIPTISH = re.compile(r"(?is)<(script|style|noscript|template|svg).*?</\1>")
_TAG = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"\s+")
_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}

# A page that renders itself into an empty container. Present in the HTML of every
# single-page app, so on its own it means little -- it counts only alongside thin text.
_SHELL_MARKERS = (
    'id="root"', "id='root'", 'id="__next"', 'id="app"', "ng-app", "data-reactroot",
)

# Serving a challenge rather than content.
_CHALLENGE_MARKERS = (
    "just a moment", "attention required", "checking your browser",
    "enable javascript and cookies to continue", "verifying you are human",
    "ddos protection by", "cf-browser-verification",
)


@dataclass
class FetchResult:
    url: str
    status: int | None
    text: str
    html_bytes: int
    error: str | None = None
    elapsed_ms: int = 0
    is_shell: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200

    @property
    def ratio(self) -> float:
        if not self.html_bytes:
            return 0.0
        return len(self.text) / self.html_bytes


def html_to_text(html: str) -> str:
    """The words a reader would see, near enough to judge the page by.

    Deliberately crude. This is not the text a quote is checked against -- it only has
    to be good enough to tell a readable page from an empty shell.
    """
    text = _SCRIPTISH.sub(" ", html)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    return _WS.sub(" ", text).strip()


def http_get(url: str, timeout: float = 20.0) -> FetchResult:
    """Fetch a URL over plain HTTP. Never raises."""
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES)
            raw = _decompress(raw, response.headers.get("Content-Encoding", ""))
            charset = response.headers.get_content_charset() or "utf-8"
            html = raw.decode(charset, errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        return FetchResult(
            url=url,
            status=exc.code,
            text="",
            html_bytes=0,
            error=f"HTTP {exc.code}",
            elapsed_ms=_ms(started),
        )
    except Exception as exc:
        return FetchResult(
            url=url,
            status=None,
            text="",
            html_bytes=0,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=_ms(started),
        )

    return FetchResult(
        url=url,
        status=status,
        text=html_to_text(html),
        html_bytes=len(html),
        elapsed_ms=_ms(started),
        is_shell=looks_like_a_shell(html),
    )


def needs_browser(result: FetchResult) -> tuple[bool, str]:
    """Whether this page has to be opened in a real browser, and why.

    The reason is returned for the agent and for the record: an escalation we cannot
    explain is one we cannot tune.
    """
    if result.error is not None:
        return True, f"plain fetch failed ({result.error})"
    if result.status != 200:
        return True, f"plain fetch returned {result.status}"

    lowered = result.text[:4_000].lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in lowered:
            return True, f"the response looks like a bot challenge ({marker!r})"

    if len(result.text) < MIN_TEXT_CHARS:
        return True, (
            f"only {len(result.text)} characters of text in "
            f"{result.html_bytes} of HTML -- the page renders itself with JavaScript"
        )
    if result.ratio < MIN_TEXT_RATIO:
        return True, (
            f"text is {result.ratio:.3f} of the HTML -- mostly script, "
            "so the content is rendered client-side"
        )
    if result.is_shell and len(result.text) < MARGINAL_TEXT_CHARS:
        return True, (
            "the page is a single-page-app container with only "
            f"{len(result.text)} characters in it, so most of it is still to render"
        )
    return False, "plain fetch returned a readable page"


def looks_like_a_shell(html: str) -> bool:
    """A single-page-app container. Only meaningful alongside thin text."""
    head = html[:200_000].lower()
    return any(marker in head for marker in _SHELL_MARKERS)


def _decompress(raw: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(raw)
        if "deflate" in encoding:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        # A truncated body (we cap the read) cannot always be decompressed. Returning
        # it as-is yields no text, which escalates -- the right outcome anyway.
        return raw
    return raw


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
