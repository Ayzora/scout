"""Playwright-backed browser session.

This is the layer that makes Scout work where raw HTML does not. Playwright renders
the page, runs its JavaScript, and we then hand the agent a *rendered* view of it.

The view is a text snapshot in which every interactive element carries a stable
``[ref=N]`` handle, in the manner of Playwright MCP. Refs beat CSS selectors for an
agent because they survive re-renders and because the agent never has to invent one:
it can only click something the snapshot showed it.

Two deliberate restraints, from the spec's section 5:

* ``robots.txt`` is honoured before any navigation.
* A bot-protection challenge is detected and *reported*, never worked around. There is
  no CAPTCHA solving and no evasion here, by design.
"""

from __future__ import annotations

import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

# Marks a page as serving a bot challenge rather than content.
_BLOCK_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "enable javascript and cookies to continue",
    "verify you are human",
    "unusual traffic",
    "px-captcha",
    "recaptcha",
    "hcaptcha",
)

# Elements worth a ref: the agent can act on these.
_INTERACTIVE_JS_ROLES = "a, button, input, select, textarea, summary, [role=button], [role=link], [role=tab], [onclick]"

# Pages an agent researching a company usually wants next.
_NAV_HINTS = (
    "about", "about-us", "company", "who-we-are", "team", "our-team", "people",
    "leadership", "management", "board", "staff", "contact", "contact-us",
    "imprint", "impressum", "legal", "careers", "jobs", "products", "services",
    "solutions", "what-we-do", "customers", "clients", "case-studies", "news",
    "press", "blog", "locations", "offices",
)

_SNAPSHOT_CHAR_CAP = 14_000

_JS_SNAPSHOT = """
() => {
  const MAXTEXT = 300;
  const seen = [];
  let ref = 0;

  const visible = (el) => {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };

  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim().slice(0, MAXTEXT);

  const name = (el) => {
    return clean(
      el.getAttribute('aria-label') ||
      el.getAttribute('alt') ||
      el.getAttribute('title') ||
      el.innerText ||
      el.value ||
      el.getAttribute('placeholder') ||
      ''
    );
  };

  // Clear refs from any previous snapshot of this page.
  document.querySelectorAll('[data-scout-ref]').forEach(e => e.removeAttribute('data-scout-ref'));

  const INTERACTIVE = %INTERACTIVE%;
  const TEXTUAL = 'h1, h2, h3, h4, h5, h6, p, li, td, th, dt, dd, blockquote, figcaption, address, label, span[class*=price], strong';

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  const out = [];
  let node = document.body;
  const all = [document.body, ...document.body.querySelectorAll('*')];

  for (const el of all) {
    if (!visible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const isInteractive = el.matches(INTERACTIVE);
    const isTextual = el.matches(TEXTUAL);
    if (!isInteractive && !isTextual) continue;

    // Skip a textual node whose text is already covered by a textual ancestor.
    if (isTextual && !isInteractive) {
      const parentTextual = el.parentElement && el.parentElement.closest(TEXTUAL);
      if (parentTextual && parentTextual !== el) continue;
    }

    const label = name(el);
    if (!label && !isInteractive) continue;

    if (isInteractive) {
      ref += 1;
      el.setAttribute('data-scout-ref', String(ref));
      const entry = { ref, tag, label, role: el.getAttribute('role') || tag };
      if (tag === 'a') entry.href = el.getAttribute('href');
      if (tag === 'input') entry.type = el.getAttribute('type') || 'text';
      if (el.disabled) entry.disabled = true;
      out.push(entry);
    } else {
      out.push({ tag, label, role: tag });
    }
  }

  return {
    title: document.title,
    url: location.href,
    bodyText: clean(document.body.innerText).slice(0, 200),
    nodes: out,
  };
}
""".replace("%INTERACTIVE%", repr(_INTERACTIVE_JS_ROLES))


@dataclass
class PageView:
    """What the agent is shown after an action."""

    url: str
    title: str
    text: str
    screenshot_path: str | None = None
    truncated: bool = False
    blocked: bool = False
    note: str | None = None

    def as_tool_result(self) -> str:
        head = f"URL: {self.url}\nTitle: {self.title or '(no title)'}"
        if self.blocked:
            return (
                f"{head}\n\nBLOCKED: this site is serving a bot-protection challenge "
                f"instead of content. Do not try to work around it. Record what you "
                f"need as unknown and continue elsewhere.\n{self.note or ''}"
            )
        parts = [head]
        if self.note:
            parts.append(f"Note: {self.note}")
        parts.append("")
        parts.append(self.text)
        if self.truncated:
            parts.append(
                "\n[snapshot truncated — scroll or open a more specific page for more]"
            )
        return "\n".join(parts)


@dataclass
class NavResult:
    view: PageView
    status: int | None = None


class RobotsCache:
    """One robots.txt parser per host, fetched at most once."""

    def __init__(self) -> None:
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str, user_agent: str = "*") -> tuple[bool, str | None]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"unsupported scheme {parsed.scheme!r}"
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(urljoin(host, "/robots.txt"))
            try:
                rp.read()
            except Exception:
                # Unreachable robots.txt is not a prohibition.
                self._cache[host] = None
            else:
                self._cache[host] = rp
        rp = self._cache[host]
        if rp is None:
            return True, None
        if rp.can_fetch(user_agent, url):
            return True, None
        return False, "disallowed by robots.txt"


def normalise_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty URL")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw.lstrip("/")
    return raw


@dataclass
class BrowserSession:
    """A browser scoped to one company's research."""

    screenshot_dir: Path
    headed: bool = True
    timeout_ms: int = 20_000
    snapshot_cap: int = _SNAPSHOT_CHAR_CAP

    _pw: Playwright | None = field(default=None, init=False, repr=False)
    _browser: object | None = field(default=None, init=False, repr=False)
    _page: Page | None = field(default=None, init=False, repr=False)
    _shot_seq: int = field(default=0, init=False)
    _robots: RobotsCache = field(default_factory=RobotsCache, init=False)
    visited: list[str] = field(default_factory=list, init=False)

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self.headed)
        context = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-GB",
        )
        context.set_default_timeout(self.timeout_ms)
        self._page = context.new_page()

    def close(self) -> None:
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass
        self._page = None
        self._browser = None
        self._pw = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("BrowserSession is not started")
        return self._page

    @property
    def current_url(self) -> str:
        return self.page.url if self._page is not None else ""

    # ---------------------------------------------------------------- capture

    def screenshot(self) -> str | None:
        """Always write a screenshot to disk, whatever the model is sent."""
        self._shot_seq += 1
        path = self.screenshot_dir / f"{self._shot_seq:04d}.png"
        try:
            self.page.screenshot(path=str(path), full_page=False)
        except PlaywrightError:
            return None
        return str(path)

    def screenshot_bytes(self) -> bytes | None:
        """PNG bytes, for the turns where the model should actually look."""
        try:
            return self.page.screenshot(full_page=False)
        except PlaywrightError:
            return None

    # --------------------------------------------------------------- snapshot

    def snapshot(self, note: str | None = None) -> PageView:
        """Render the current page as agent-readable text with ``[ref=N]`` handles."""
        try:
            data = self.page.evaluate(_JS_SNAPSHOT)
        except PlaywrightError as exc:
            return PageView(
                url=self.current_url,
                title="",
                text=f"(could not read page: {exc})",
                screenshot_path=self.screenshot(),
                note=note,
            )

        blocked, reason = self._detect_block(data)
        lines: list[str] = []
        for n in data.get("nodes", []):
            label = n.get("label") or ""
            if n.get("ref"):
                bits = [f"[ref={n['ref']}]", f"<{n.get('role') or n['tag']}>"]
                if label:
                    bits.append(label)
                if n.get("href"):
                    bits.append(f"-> {n['href']}")
                if n.get("type"):
                    bits.append(f"(input type={n['type']})")
                if n.get("disabled"):
                    bits.append("(disabled)")
                lines.append(" ".join(bits))
            elif label:
                lines.append(f"<{n['tag']}> {label}")

        body = "\n".join(lines)
        truncated = len(body) > self.snapshot_cap
        if truncated:
            body = body[: self.snapshot_cap]

        return PageView(
            url=data.get("url") or self.current_url,
            title=data.get("title") or "",
            text=body or "(page has no readable content)",
            screenshot_path=self.screenshot(),
            truncated=truncated,
            blocked=blocked,
            note=reason or note,
        )

    def _detect_block(self, data: dict) -> tuple[bool, str | None]:
        haystack = " ".join(
            [str(data.get("title") or ""), str(data.get("bodyText") or "")]
        ).lower()
        for marker in _BLOCK_MARKERS:
            if marker in haystack:
                return True, f"challenge marker detected: {marker!r}"
        return False, None

    # ------------------------------------------------------------- navigation

    def goto(self, raw_url: str) -> NavResult:
        url = normalise_url(raw_url)
        allowed, why = self._robots.allowed(url)
        if not allowed:
            return NavResult(
                view=PageView(
                    url=url,
                    title="",
                    text=f"Navigation refused: {why}. Respect it and try another source.",
                    blocked=True,
                    note=why,
                )
            )

        status: int | None = None
        try:
            response = self.page.goto(url, wait_until="domcontentloaded")
            status = response.status if response is not None else None
            # Give client-rendered pages a moment to paint.
            try:
                self.page.wait_for_load_state("networkidle", timeout=4_000)
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            return NavResult(
                view=self.snapshot(note=f"navigation to {url} timed out; showing what loaded"),
                status=None,
            )
        except PlaywrightError as exc:
            return NavResult(
                view=PageView(
                    url=url, title="", text=f"Navigation failed: {exc}", note=str(exc)
                ),
                status=None,
            )

        self.visited.append(url)
        note = None
        if status is not None and status >= 400:
            note = f"HTTP {status}"
        view = self.snapshot(note=note)
        if status in (401, 403, 429):
            view.blocked = True
            view.note = f"HTTP {status} — access refused. Do not retry aggressively."
        return NavResult(view=view, status=status)

    def back(self) -> PageView:
        try:
            self.page.go_back(wait_until="domcontentloaded")
        except PlaywrightError as exc:
            return self.snapshot(note=f"could not go back: {exc}")
        return self.snapshot()

    # ----------------------------------------------------------- interactions

    def _locator(self, ref: int):
        return self.page.locator(f'[data-scout-ref="{ref}"]')

    def click(self, ref: int) -> PageView:
        loc = self._locator(ref)
        try:
            if loc.count() == 0:
                return self.snapshot(
                    note=f"ref {ref} is no longer on the page; take a fresh snapshot"
                )
            loc.first.click(timeout=self.timeout_ms)
            try:
                self.page.wait_for_load_state("networkidle", timeout=4_000)
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            return self.snapshot(note=f"click on ref {ref} timed out")
        except PlaywrightError as exc:
            return self.snapshot(note=f"click on ref {ref} failed: {exc}")
        return self.snapshot()

    def type_text(self, ref: int, text: str, submit: bool = False) -> PageView:
        loc = self._locator(ref)
        try:
            if loc.count() == 0:
                return self.snapshot(note=f"ref {ref} is no longer on the page")
            loc.first.fill(text, timeout=self.timeout_ms)
            if submit:
                loc.first.press("Enter")
                try:
                    self.page.wait_for_load_state("networkidle", timeout=6_000)
                except PlaywrightTimeout:
                    pass
        except PlaywrightError as exc:
            return self.snapshot(note=f"typing into ref {ref} failed: {exc}")
        return self.snapshot()

    def scroll(self, direction: str = "down", amount: int = 1) -> PageView:
        delta = 800 * max(1, amount) * (-1 if direction == "up" else 1)
        try:
            self.page.mouse.wheel(0, delta)
            time.sleep(0.3)
        except PlaywrightError as exc:
            return self.snapshot(note=f"scroll failed: {exc}")
        return self.snapshot()

    # ------------------------------------------------------------------ links

    def find_links(self, pattern: str | None = None) -> list[dict[str, str]]:
        """Candidate next pages, ranked so research-relevant ones come first."""
        try:
            raw = self.page.eval_on_selector_all(
                "a[href]",
                """els => els.map(e => ({
                     text: (e.innerText || e.getAttribute('aria-label') || '').replace(/\\s+/g,' ').trim().slice(0,120),
                     href: e.href
                   }))""",
            )
        except PlaywrightError:
            return []

        host = urlparse(self.current_url).netloc
        seen: set[str] = set()
        scored: list[tuple[int, dict[str, str]]] = []
        needle = (pattern or "").lower().strip()

        for item in raw:
            href = (item.get("href") or "").split("#")[0]
            if not href or href in seen:
                continue
            if urlparse(href).scheme not in ("http", "https"):
                continue
            seen.add(href)
            text = item.get("text") or ""
            blob = f"{href} {text}".lower()

            if needle and needle not in blob:
                continue

            score = 0
            if urlparse(href).netloc == host:
                score += 5
            for hint in _NAV_HINTS:
                if hint in blob:
                    score += 10
                    break
            scored.append((score, {"text": text, "url": href}))

        scored.sort(key=lambda pair: -pair[0])
        return [item for _, item in scored[:40]]
