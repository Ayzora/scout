"""The agent's tool surface.

Tools are built by a factory so each one closes over this company's browser session and
recorder. That keeps per-company state out of module globals and makes the whole surface
straightforward to construct in a test.

A tool returns either a string or a list of content blocks. We use the list form only
when the agent asked for a screenshot, which is how vision stays selective: the harness
writes a screenshot to disk on *every* step for the dashboard, but only sends one to the
model when asked.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Literal

from anthropic import beta_tool

from scout.agent.recorder import Recorder
from scout.browser.session import BrowserSession, PageView
from scout.config import Settings

WEB_SEARCH_MAX_USES = 8
WEB_FETCH_MAX_USES = 8

SERVER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": WEB_SEARCH_MAX_USES,
    },
    {
        "type": "web_fetch_20260209",
        "name": "web_fetch",
        "max_uses": WEB_FETCH_MAX_USES,
        "citations": {"enabled": True},
    },
]


def _image_block(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        },
    }


def build_tools(
    *,
    session: BrowserSession,
    recorder: Recorder,
    settings: Settings,
) -> list[Any]:
    """Construct the agent's client-side tools, bound to this company's context."""

    def _budget_exhausted() -> str | None:
        if recorder.step_count >= settings.max_steps:
            return (
                f"Step limit of {settings.max_steps} reached. Record anything "
                "outstanding as unknown and call finish_research now."
            )
        return None

    def _deliver(
        view: PageView,
        *,
        tool_name: str,
        tool_input: dict,
        include_screenshot: bool,
        started: float,
    ) -> str | list[dict[str, Any]]:
        """Persist the step, update page context, and shape the tool result."""
        recorder.page.url = view.url
        recorder.page.title = view.title
        recorder.page.screenshot_path = view.screenshot_path
        if view.url and not view.blocked:
            recorder.visited_urls.add(view.url)

        recorder.log_step(
            step_type="tool_result",
            tool_name=tool_name,
            tool_input=tool_input,
            output_summary=view.as_tool_result()[:2000],
            url=view.url,
            screenshot_path=view.screenshot_path,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        text = view.as_tool_result()
        if not include_screenshot:
            return text
        png = session.screenshot_bytes()
        if png is None:
            return text + "\n\n[screenshot unavailable]"
        return [{"type": "text", "text": text}, _image_block(png)]

    # ------------------------------------------------------------------ browser

    @beta_tool
    def browser_goto(
        url: str, include_screenshot: bool = False
    ) -> str | list[dict[str, Any]]:
        """Open a URL in the browser and return the rendered page.

        The page is returned as text in which every interactive element carries a
        [ref=N] handle you can pass to browser_click or browser_type.

        Args:
            url: Absolute URL, or a bare domain such as example.com.
            include_screenshot: Also return an image of the page. Use only when layout
                or imagery carries the information; it is far more expensive than text.
        """
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        result = session.goto(url)
        return _deliver(
            result.view,
            tool_name="browser_goto",
            tool_input={"url": url, "include_screenshot": include_screenshot},
            include_screenshot=include_screenshot,
            started=started,
        )

    @beta_tool
    def browser_snapshot(
        include_screenshot: bool = False,
    ) -> str | list[dict[str, Any]]:
        """Re-read the page that is currently open.

        Use this after the page changes, or when a [ref=N] handle you were given is no
        longer valid.

        Args:
            include_screenshot: Also return an image of the page.
        """
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        return _deliver(
            session.snapshot(),
            tool_name="browser_snapshot",
            tool_input={"include_screenshot": include_screenshot},
            include_screenshot=include_screenshot,
            started=started,
        )

    @beta_tool
    def browser_click(
        ref: int, include_screenshot: bool = False
    ) -> str | list[dict[str, Any]]:
        """Click an element and return the resulting page.

        Args:
            ref: The number from a [ref=N] handle in the current snapshot.
            include_screenshot: Also return an image of the resulting page.
        """
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        return _deliver(
            session.click(ref),
            tool_name="browser_click",
            tool_input={"ref": ref, "include_screenshot": include_screenshot},
            include_screenshot=include_screenshot,
            started=started,
        )

    @beta_tool
    def browser_type(
        ref: int, text: str, submit: bool = False
    ) -> str | list[dict[str, Any]]:
        """Type into an input or textarea.

        Use this for on-site search boxes. Do not use it to submit contact or enquiry
        forms that would send a message to the company.

        Args:
            ref: The number from a [ref=N] handle for an input element.
            text: The text to enter.
            submit: Press Enter afterwards.
        """
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        return _deliver(
            session.type_text(ref, text, submit=submit),
            tool_name="browser_type",
            tool_input={"ref": ref, "text": text, "submit": submit},
            include_screenshot=False,
            started=started,
        )

    @beta_tool
    def browser_scroll(
        direction: Literal["down", "up"] = "down", amount: int = 1
    ) -> str | list[dict[str, Any]]:
        """Scroll the page and return what is now visible.

        Args:
            direction: Scroll "down" or "up".
            amount: Number of screen-heights to scroll.
        """
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        return _deliver(
            session.scroll(direction, amount),
            tool_name="browser_scroll",
            tool_input={"direction": direction, "amount": amount},
            include_screenshot=False,
            started=started,
        )

    @beta_tool
    def browser_back() -> str | list[dict[str, Any]]:
        """Go back to the previous page."""
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        return _deliver(
            session.back(),
            tool_name="browser_back",
            tool_input={},
            include_screenshot=False,
            started=started,
        )

    @beta_tool
    def browser_find_links(pattern: str | None = None) -> str:
        """List links on the current page, with research-relevant pages ranked first.

        Prefer this over guessing URLs such as /about or /team, which often do not exist.

        Args:
            pattern: Optional case-insensitive filter on the link text or URL.
        """
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        links = session.find_links(pattern)
        if not links:
            body = "No links found on this page."
        else:
            body = "\n".join(
                f"- {item['text'] or '(no text)'} -> {item['url']}" for item in links
            )
        recorder.log_step(
            step_type="tool_result",
            tool_name="browser_find_links",
            tool_input={"pattern": pattern},
            output_summary=body[:2000],
            url=session.current_url,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return body

    # ---------------------------------------------------------------- recording

    @beta_tool
    def record_finding(
        field: str,
        value: str,
        kind: Literal["fact", "inference"],
        evidence_url: str,
        evidence_quote: str,
        confidence: float | None = None,
        reason: str | None = None,
    ) -> str:
        """Record one piece of information about the company, with its source.

        Call this the moment you observe something, while the page is still open. You may
        call it several times for the same field -- for a list of products, one call per
        product is better than one call with everything in a single string.

        Args:
            field: The field being populated, such as industry or headquarters_location.
            value: The value, as a short readable string.
            kind: "fact" if the page states it directly, "inference" if you are
                concluding it from indirect signals.
            evidence_url: The URL of the page you actually saw this on.
            evidence_quote: The wording on that page that supports it. Quote it; do not
                paraphrase.
            confidence: Optional 0.0-1.0 confidence, most useful for inferences.
            reason: For an inference, how you reached it.
        """
        try:
            finding_id, warning = recorder.record_finding(
                field_name=field,
                value=value,
                kind=kind,
                evidence_url=evidence_url,
                evidence_quote=evidence_quote,
                confidence=confidence,
                reason=reason,
            )
        except ValueError as exc:
            return f"Not recorded: {exc}"

        recorder.log_step(
            step_type="tool_result",
            tool_name="record_finding",
            tool_input={"field": field, "value": value, "kind": kind},
            output_summary=f"finding {finding_id} recorded"
            + (f" (warning: {warning})" if warning else ""),
            url=evidence_url,
        )
        msg = f"Recorded {kind} for {field!r} (finding {finding_id})."
        if warning:
            msg += f" WARNING: {warning}"
        return msg

    @beta_tool
    def mark_unknown(field: str, reason: str) -> str:
        """Record that a requested field could not be established.

        Use this rather than guessing or leaving the field unrecorded. An explicit
        unknown is a useful result.

        Args:
            field: The field you could not establish.
            reason: Why not -- where you looked and what was missing.
        """
        finding_id = recorder.mark_unknown(field_name=field, reason=reason)
        recorder.log_step(
            step_type="tool_result",
            tool_name="mark_unknown",
            tool_input={"field": field, "reason": reason},
            output_summary=f"unknown {finding_id} recorded",
        )
        return f"Recorded {field!r} as unknown (finding {finding_id})."

    @beta_tool
    def record_person(
        name: str,
        evidence_url: str,
        evidence_quote: str,
        role: str | None = None,
        seniority: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> str:
        """Record a decision-maker the company itself publishes.

        Only record a person named on a page the company publishes: a team page, press
        release, imprint or public filing. Record contact details only where that page
        publishes them. Never construct an email address from a pattern, and never record
        a personal phone number or home address.

        Args:
            name: The person's name as published.
            evidence_url: The URL of the page naming them.
            evidence_quote: The wording on that page naming them and their role.
            role: Their job title as published.
            seniority: Optional grouping such as founder, executive, or department head.
            email: A business email address only if that page publishes it.
            phone: A business phone number only if that page publishes it.
        """
        contact = {k: v for k, v in (("email", email), ("phone", phone)) if v}
        person_id, warning = recorder.record_person(
            name=name,
            role=role,
            seniority=seniority,
            contact=contact,
            evidence_url=evidence_url,
            evidence_quote=evidence_quote,
        )
        recorder.log_step(
            step_type="tool_result",
            tool_name="record_person",
            tool_input={"name": name, "role": role},
            output_summary=f"person {person_id} recorded"
            + (f" (warning: {warning})" if warning else ""),
            url=evidence_url,
        )
        msg = f"Recorded {name!r} (person {person_id})."
        if warning:
            msg += f" WARNING: {warning}"
        return msg

    @beta_tool
    def finish_research(summary: str) -> str:
        """Declare the research complete. Call this exactly once, at the end.

        Args:
            summary: A short account of what you established and what you could not.
        """
        recorder.finished = True
        recorder.final_summary = summary
        recorder.log_step(
            step_type="message",
            tool_name="finish_research",
            output_summary=summary[:2000],
        )
        stats = recorder.stats()
        return (
            "Research closed. "
            f"{stats['facts']} facts, {stats['inferences']} inferences, "
            f"{stats['unknowns']} unknowns across {stats['steps']} steps. "
            "Do not call any more tools."
        )

    return [
        browser_goto,
        browser_snapshot,
        browser_click,
        browser_type,
        browser_scroll,
        browser_back,
        browser_find_links,
        record_finding,
        mark_unknown,
        record_person,
        finish_research,
    ]
