"""Scout's Playwright browser behind Anthropic's own browser toolset interface.

The tools Scout defines by hand (`browser_goto`, `browser_snapshot`, ...) and
`browser_toolset_20260801` are the same idea: give the model page text with stable
element references instead of pixel coordinates. Scout's snapshot renders
``[ref=4] <a> Careers -> /careers.html``; the toolset's `read_page` renders
``link "Careers" [ref_4]``. One is a private invention and the other is the interface
the model has seen most of, which is a reason to measure them against each other
rather than to assume either is better.

This is a *client* toolset: Anthropic defines the member names and schemas, and this
class executes every call against the same `BrowserSession` the hand-written tools use.
So the snapshot filtering, the screenshots written to disk, the `robots.txt` check and
the block detection are all unchanged, and the only variable in the comparison is the
vocabulary the model works in.

Only the members research needs are implemented. The SDK marks the rest disabled and
never dispatches them, so the unimplemented majority of the 31 costs nothing and cannot
be called.
"""

from __future__ import annotations

import base64
from typing import Any

# Imported from a private module because the SDK's browser toolset classes are still
# beta and `anthropic.lib.tools` does not re-export them. An SDK upgrade can therefore
# move these; tests/test_browser_toolset.py asserts the path still resolves so that
# shows up as a clear failure rather than at run time on a paid run.
from anthropic.lib.tools._toolsets._base import BetaScreenshotResult
from anthropic.lib.tools._toolsets._browser import (
    BetaAbstractBrowserToolset20260801,
)
from anthropic.lib.tools._toolsets._results import (
    BetaBrowserNavigateResult,
    BetaBrowserState,
)

from scout.agent.recorder import Recorder
from scout.browser.session import BrowserSession, PageView

TAB_ID = "scout"


def _ref_number(ref: str) -> int:
    """``ref_12`` -> 12.

    The toolset names references ``ref_N`` while Scout's snapshot numbers elements
    directly, so the two have to be translated rather than assumed compatible.
    """
    digits = "".join(c for c in str(ref) if c.isdigit())
    if not digits:
        raise ValueError(f"not a usable element reference: {ref!r}")
    return int(digits)


def render_page(view: PageView) -> str:
    """One page as `read_page` reports it.

    Scout's own renderer emits ``[ref=4] <a> Careers -> /careers.html``. The toolset's
    convention puts the role first and the reference last, which is what the model
    expects here, so the same underlying snapshot is re-rendered rather than reused.
    """
    lines: list[str] = []
    for raw in view.text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[ref="):
            marker, _, rest = line.partition("]")
            number = marker.removeprefix("[ref=")
            rest = rest.strip()
            role = "element"
            if rest.startswith("<"):
                role, _, rest = rest.removeprefix("<").partition(">")
                rest = rest.strip()
            label, _, href = rest.partition("->")
            label = label.strip()
            entry = f'{role} "{label}" [ref_{number}]'
            if href.strip():
                entry += f" -> {href.strip()}"
            lines.append(entry)
        elif line.startswith("<"):
            tag, _, text = line.removeprefix("<").partition(">")
            lines.append(f"{tag}: {text.strip()}")
        else:
            lines.append(line)
    body = "\n".join(lines) or "(page has no readable content)"
    header = f"{view.url}\n{view.title or '(no title)'}"
    if view.blocked:
        return (
            f"{header}\n\nBLOCKED: this site is serving a bot-protection challenge "
            "instead of content. Do not try to work around it."
        )
    if view.note:
        header += f"\n[{view.note}]"
    return f"{header}\n\n{body}"


class ScoutBrowserToolset(BetaAbstractBrowserToolset20260801):
    """Anthropic's browser members, executed against Scout's own browser."""

    def __init__(self, *, session: BrowserSession, recorder: Recorder) -> None:
        super().__init__(
            # The toolset definition is part of the cached prefix, like the
            # hand-written tool list it replaces.
            tool_configs={"cache_control": {"type": "ephemeral"}},
            # robots.txt is checked inside BrowserSession.goto as well; this refuses
            # before the driver is touched so a disallowed URL never loads.
            url_policy=self._check_url,
        )
        self.session = session
        self.recorder = recorder
        self._last: PageView | None = None

    # ------------------------------------------------------------------ plumbing

    def _check_url(self, _context: Any, url: str) -> None:
        allowed, why = self.session._robots.allowed(url)
        if not allowed:
            raise ValueError(f"{why}. Respect it and try another source.")

    def _browser_state(self, _context: Any) -> BetaBrowserState:
        """Reported after every call, including refused ones."""
        view = self._last
        return BetaBrowserState(
            tabs=[
                {
                    "tab_id": TAB_ID,
                    "title": (view.title if view else "") or "",
                    "url": (view.url if view else self.session.current_url) or "",
                    "active": True,
                }
            ]
        )

    def _deliver(self, view: PageView, *, member: str, **tool_input: Any) -> None:
        """Keep Scout's own bookkeeping in step, whichever interface drove the call.

        The step trail, the page context the provenance guard reads and the
        always-on screenshot have to behave identically across engines, or the
        comparison would measure the bookkeeping rather than the browser.
        """
        self._last = view
        self.recorder.page.url = view.url
        self.recorder.page.title = view.title
        self.recorder.page.screenshot_path = view.screenshot_path
        if view.url:
            self.recorder.visited_urls.add(view.url)
        self.recorder.log_step(
            step_type="tool_result",
            tool_name=f"browser.{member}",
            tool_input=tool_input,
            output_summary=(view.note or view.title or "")[:400] or None,
            url=view.url,
            screenshot_path=view.screenshot_path,
        )

    # ------------------------------------------------------------------- members

    def navigate(self, _context: Any, input: Any) -> BetaBrowserNavigateResult:
        url = getattr(input, "url", None) or ""
        if url in ("back", "forward", "reload"):
            # Only `back` is meaningful for research; the others are not worth a
            # second code path.
            view = self.session.back() if url == "back" else self.session.snapshot()
            status = None
        else:
            result = self.session.goto(url)
            view, status = result.view, result.status
        self._deliver(view, member="navigate", url=url)
        return BetaBrowserNavigateResult(
            url=view.url, status=status, title=view.title or None
        )

    def read_page(self, _context: Any, input: Any) -> str:
        view = self.session.snapshot()
        self._deliver(view, member="read_page")
        return render_page(view)

    def get_page_text(self, _context: Any, _input: Any) -> str:
        """The page's prose, unfiltered.

        `read_page` returns the filtered snapshot that Scout pays for; this returns the
        whole text for when the agent needs what the filter removed.
        """
        text = self.session.full_text()
        self.recorder.log_step(
            step_type="tool_result",
            tool_name="browser.get_page_text",
            tool_input={},
            output_summary=f"{len(text)} characters",
            url=self.session.current_url,
        )
        return text or "(page has no readable text)"

    def find(self, _context: Any, input: Any) -> str:
        """Locate text on the page, and surface candidate research pages with it."""
        query = getattr(input, "query", "") or ""
        links = self.session.find_links(query)
        text = self.session.full_text()
        hits = [
            line.strip()
            for line in text.splitlines()
            if query.lower() in line.lower() and line.strip()
        ][:10]
        self.recorder.log_step(
            step_type="tool_result",
            tool_name="browser.find",
            tool_input={"query": query},
            output_summary=f"{len(hits)} text matches, {len(links)} links",
            url=self.session.current_url,
        )
        parts = []
        if links:
            parts.append("Links:\n" + "\n".join(f"- {u}" for u in links[:10]))
        if hits:
            parts.append("Text:\n" + "\n".join(f"- {h[:200]}" for h in hits))
        return "\n\n".join(parts) or f"Nothing matching {query!r} on this page."

    def left_click(self, _context: Any, input: Any) -> str | None:
        target = getattr(input, "target", None)
        ref = getattr(target, "ref", None)
        if ref is None:
            # Coordinate clicking is the thing this design exists to avoid: a pixel
            # target does not survive a re-render, where an element reference does.
            raise ValueError(
                "this browser clicks by element reference, not by coordinate. "
                "Call read_page and click a ref_N it returned."
            )
        view = self.session.click(_ref_number(ref))
        self._deliver(view, member="left_click", ref=ref)
        return f"Clicked {ref}."

    # `type` is deliberately not implemented. It sends keystrokes wherever focus
    # happens to be, which Scout's session cannot express: its typing tool takes the
    # field's element reference. Leaving it out means the SDK disables it and the model
    # reaches for `form_input` instead, rather than being told no after calling it.

    def form_input(self, _context: Any, input: Any) -> str | None:
        target = getattr(input, "target", None)
        ref = getattr(target, "ref", None)
        value = getattr(input, "value", "")
        if ref is None:
            raise ValueError("form_input needs a ref_N target.")
        view = self.session.type_text(_ref_number(ref), str(value), submit=False)
        self._deliver(view, member="form_input", ref=ref, value=str(value)[:80])
        return f"Entered text into {ref}."

    def scroll(self, _context: Any, input: Any) -> str | None:
        direction = getattr(input, "scroll_direction", "down")
        view = self.session.scroll(direction)
        self._deliver(view, member="scroll", direction=direction)
        return f"Scrolled {direction}."

    def screenshot(self, _context: Any, _input: Any) -> BetaScreenshotResult:
        data = self.session.screenshot_bytes()
        return BetaScreenshotResult(
            data=base64.standard_b64encode(data).decode(), media_type="image/png"
        )
