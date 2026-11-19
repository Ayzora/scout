"""Check recorded quotes against the pages they were taken from.

Scout stores a source URL and a supporting quote for every claim, and already warns
when the agent cites a page it never opened. Nothing until now checked the quote
itself, which is the failure that matters: a model asked for a source will readily
produce a reachable URL attached to a sentence that is not on it -- paraphrased,
misremembered, or carried over from a different page. That looks correct in the output
and in the CSV.

Verification uses no model, so it is free and can run after every research run. It
reads the full rendered page rather than the filtered snapshot the agent saw, because
the quote is a claim about the page, not about our view of it.

Known limit: content that only exists after an interaction has no URL that reproduces
it, so a quote taken from behind a click cannot be re-verified by loading the URL
again. Those come back as NOT FOUND and the report says so rather than quietly
counting them as errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

from scout.browser.session import BrowserSession
from scout.config import Settings
from scout.db import models as m
from scout.db.session import session_scope

# Above this, a quote differing only in wording is treated as a paraphrase rather than
# an invention. Chosen to be clearly above the ratio that unrelated prose of similar
# length produces, and tested against both ends in tests/test_verify.py.
PARAPHRASE_RATIO = 0.75

_WS = re.compile(r"\s+")
_REF_MARKER = re.compile(r"\[ref=\d+\]")
_ROLE_TAG = re.compile(r"<[a-z][a-z0-9-]*>")
_HREF_ARROW = re.compile(r"\s*->\s*\S+")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_MD_BULLET = re.compile(r"(?m)^\s*[-*+]\s+")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|`)")
# Typographic variants a page renders one way and a model reproduces another.
_SUBSTITUTIONS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    " ": " ", "…": "...", "­": "",
}


class Verdict(str, Enum):
    VERBATIM = "verbatim"
    PARAPHRASE = "paraphrase"
    NOT_FOUND = "not_found"
    UNREACHABLE = "unreachable"
    NO_QUOTE = "no_quote"


def normalise(text: str) -> str:
    """Lower-case, collapse whitespace, and fold typographic variants."""
    for bad, good in _SUBSTITUTIONS.items():
        text = text.replace(bad, good)
    return _WS.sub(" ", text).strip().lower()


def strip_markdown(quote: str) -> str:
    """Reduce Markdown syntax to the words it wraps.

    ``web_fetch`` returns pages as Markdown, so a quote taken that way reads
    ``### Products - [Current accounts](https://...)`` while the browser's rendered
    text has only the words. Removing the syntax compares the prose rather than the
    representation it happened to arrive in. Only syntax goes -- every word survives,
    so this cannot turn an invented quote into a match.
    """
    quote = _MD_LINK.sub(r"\1", quote)
    quote = _MD_HEADING.sub("", quote)
    quote = _MD_BULLET.sub("", quote)
    quote = _MD_EMPHASIS.sub("", quote)
    return quote


def strip_snapshot_artifacts(quote: str) -> str:
    """Remove our own snapshot rendering from a quote.

    The agent quotes what it was shown, and a snapshot line reads
    ``[ref=4] <a> Careers -> /careers.html``. The ref, the role tag and the arrow plus
    href are this program's formatting, not the page's words, so matching them against
    page text would mark a perfectly honest quote as fabricated.
    """
    quote = _REF_MARKER.sub("", quote)
    quote = _ROLE_TAG.sub("", quote)
    quote = _HREF_ARROW.sub("", quote)
    return strip_markdown(quote).strip(" -")


def _segments(quote: str) -> list[str]:
    """Split a quote the agent elided with "..." into the parts it kept.

    An elided quote is still a quote; each surviving run of words has to appear on
    the page, but they need not be adjacent.
    """
    parts = [p.strip() for p in _ELLIPSIS.split(quote)]
    # Fragments too short to be distinctive match almost anything.
    return [p for p in parts if len(p.split()) >= 3] or [quote]


def best_match_ratio(quote: str, page: str) -> float:
    """Similarity of the quote to the most similar same-length run of page text.

    Comparing against the whole page would wash out a short quote, so this slides a
    window of roughly the quote's length and keeps the best score.
    """
    q_words = quote.split()
    p_words = page.split()
    if not q_words or not p_words:
        return 0.0

    width = len(q_words)
    if width >= len(p_words):
        return SequenceMatcher(None, quote, page).ratio()

    # A quarter of the quote's length keeps the scan cheap without stepping over the
    # best alignment.
    step = max(1, width // 4)
    matcher = SequenceMatcher(None, quote, "", autojunk=False)
    best = 0.0
    for start in range(0, len(p_words) - width + 1, step):
        window = " ".join(p_words[start : start + width])
        matcher.set_seq2(window)
        # real_quick_ratio and quick_ratio are cheap upper bounds; skip windows that
        # cannot beat the best score so far.
        if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
            continue
        best = max(best, matcher.ratio())
    return best


def classify(quote: str | None, page_text: str | None) -> tuple[Verdict, float]:
    """Decide how well a quote is supported by a page's text."""
    if not quote or not quote.strip():
        return Verdict.NO_QUOTE, 0.0
    if page_text is None:
        return Verdict.UNREACHABLE, 0.0

    page = normalise(page_text)
    cleaned = strip_snapshot_artifacts(quote)
    if not cleaned.strip():
        return Verdict.NO_QUOTE, 0.0

    # An elided quote passes only if every part it kept is on the page, so the
    # verdict and score come from its weakest segment.
    worst_verdict, worst_ratio = Verdict.VERBATIM, 1.0
    for segment in _segments(cleaned):
        q = normalise(segment)
        if q in page:
            continue
        ratio = best_match_ratio(q, page)
        verdict = (
            Verdict.PARAPHRASE if ratio >= PARAPHRASE_RATIO else Verdict.NOT_FOUND
        )
        if ratio < worst_ratio:
            worst_verdict, worst_ratio = verdict, ratio
    return worst_verdict, worst_ratio


@dataclass
class QuoteCheck:
    company_run_id: int
    domain: str
    field: str
    kind: str
    source_url: str
    quote: str | None
    verdict: Verdict
    ratio: float


@dataclass
class VerifyReport:
    checks: list[QuoteCheck]
    pages_loaded: int
    pages_unreachable: int

    def counts(self) -> dict[str, int]:
        out = {v.value: 0 for v in Verdict}
        for c in self.checks:
            out[c.verdict.value] += 1
        return out

    @property
    def accuracy(self) -> float | None:
        """Share of quotes found on their cited page, verbatim or paraphrased.

        ``None`` when nothing could be checked, so an empty run reports "no data"
        rather than a perfect or a zero score.
        """
        checkable = [
            c
            for c in self.checks
            if c.verdict
            in (Verdict.VERBATIM, Verdict.PARAPHRASE, Verdict.NOT_FOUND)
        ]
        if not checkable:
            return None
        found = sum(
            1
            for c in checkable
            if c.verdict in (Verdict.VERBATIM, Verdict.PARAPHRASE)
        )
        return found / len(checkable)


def _evidence_rows(settings: Settings, run_id: int) -> list[tuple]:
    """(company_run_id, domain, field, kind, source_url, quote) for a run."""
    with session_scope(settings) as s:
        rows = (
            s.query(
                m.CompanyRun.id,
                m.Company.domain,
                m.Finding.field,
                m.Finding.kind,
                m.Evidence.source_url,
                m.Evidence.quote,
            )
            .select_from(m.Run)
            .join(m.CompanyRun, m.CompanyRun.run_id == m.Run.id)
            .join(m.Company, m.Company.id == m.CompanyRun.company_id)
            .join(m.Finding, m.Finding.company_run_id == m.CompanyRun.id)
            .join(m.Evidence, m.Evidence.finding_id == m.Finding.id)
            .filter(m.Run.id == run_id)
            .filter(m.Finding.kind != m.FindingKind.UNKNOWN.value)
            .all()
        )
        return [tuple(r) for r in rows]


def verify_run(
    settings: Settings,
    run_id: int,
    *,
    session: BrowserSession | None = None,
    on_page=None,
) -> VerifyReport:
    """Re-read every cited page in a run and check the quotes against it.

    Each distinct URL is loaded once, however many claims cite it.
    """
    rows = _evidence_rows(settings, run_id)
    by_url: dict[str, list[tuple]] = {}
    for row in rows:
        by_url.setdefault(row[4], []).append(row)

    checks: list[QuoteCheck] = []
    loaded = unreachable = 0

    def run_with(browser: BrowserSession) -> None:
        nonlocal loaded, unreachable
        for url, group in by_url.items():
            page_text: str | None = None
            if url:
                result = browser.goto(url)
                if result.view.blocked or result.view.text.startswith(
                    ("Navigation failed", "Navigation refused")
                ):
                    unreachable += 1
                else:
                    page_text = browser.full_text()
                    loaded += 1
            if on_page is not None:
                on_page(url, page_text is not None)
            for cr_id, domain, field, kind, source_url, quote in group:
                verdict, ratio = classify(quote, page_text)
                checks.append(
                    QuoteCheck(
                        company_run_id=cr_id,
                        domain=domain,
                        field=field,
                        kind=kind,
                        source_url=source_url,
                        quote=quote,
                        verdict=verdict,
                        ratio=ratio,
                    )
                )

    if session is not None:
        run_with(session)
    else:
        with BrowserSession(
            screenshot_dir=settings.screenshot_dir,
            headed=False,
            timeout_ms=settings.page_timeout_ms,
            # Verification re-reads pages the run already read. Holding it to a
            # stricter rule than the run would report its own evidence unreachable.
            respect_robots=settings.respect_robots,
        ) as browser:
            run_with(browser)

    return VerifyReport(
        checks=checks, pages_loaded=loaded, pages_unreachable=unreachable
    )
