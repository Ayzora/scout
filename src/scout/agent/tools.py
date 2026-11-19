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
import json
import time
from typing import Any, Literal

from anthropic import beta_tool
from pydantic import BaseModel

from scout.agent.recorder import Recorder
from scout.browser.fetcher import http_get, needs_browser
from scout.registry import lookup as registry_lookup
from scout.browser.session import BrowserSession, PageView, normalise_url
from scout.config import Settings

# Per branch, not per company. Searches are billed per request ($10 per 1,000), which
# was rounding error on an expensive model and is a material share of the bill on a
# cheap one -- so with several branches the per-branch allowance has to come down or
# search becomes the largest line in a deep run.
WEB_SEARCH_MAX_USES = 4

# What a branch gets once earlier branches have handed it what they found. A search is
# billed per request, not per token, so at Haiku prices ten searches cost five times
# everything else in a run put together -- the one line item where a cap is the only
# lever.
WEB_SEARCH_MAX_USES_WITH_DOSSIER = 2

WEB_FETCH_MAX_USES = 6

# Unset, a single fetch can put an unbounded page into context. Roughly matched to
# the browser's snapshot cap so the two engines cost comparable amounts per page.
WEB_FETCH_MAX_CONTENT_TOKENS = 4_000

# A directly fetched page enters context as text, so it needs the same ceiling the
# browser snapshot has. Roughly matched to it, in characters rather than tokens.
_FETCH_TEXT_CAP = 8_000


class FindingInput(BaseModel):
    """One observed claim, as the agent reports it."""

    field: str
    value: str
    kind: Literal["fact", "inference"]
    evidence_url: str
    evidence_quote: str
    confidence: float | None = None
    reason: str | None = None


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
        "max_content_tokens": WEB_FETCH_MAX_CONTENT_TOKENS,
        "citations": {"enabled": True},
    },
]


def server_tools(max_searches: int = WEB_SEARCH_MAX_USES) -> list[dict[str, Any]]:
    """The server-side tools, with this branch's search allowance.

    Search is the one cost that no amount of caching touches: it is billed per request.
    A branch that has been told what earlier branches found needs fewer of them, so the
    allowance is an argument rather than a constant.
    """
    tools = [dict(t) for t in SERVER_TOOLS]
    tools[0]["max_uses"] = max(1, max_searches)
    return tools


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
    session: BrowserSession | None,
    recorder: Recorder,
    settings: Settings,
) -> list[Any]:
    """Construct the agent's client-side tools, bound to this company's context.

    The engine is a choice of tool surface: ``browser`` adds the Playwright tools,
    ``fetch`` works through the server-side tools alone. Both get the same recording
    surface, so their findings are directly comparable.
    """
    if settings.engine in ("browser", "browser_toolset") and session is None:
        raise ValueError(f"the {settings.engine} engine needs a BrowserSession")

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

    @beta_tool
    def lookup_registry(name: str) -> str:
        """Ask a statutory register for an organisation, by name.

        Free, exact, and the strongest source there is for legal identity: the
        registered name, the registration number, whether the entity is still live,
        and its registered postcode. Use it before searching the web for any of those.

        Returns one entry per match, each with the register's own URL. An entity whose
        status is not active is a former name -- aggregators keep those long after the
        register has retired them, so check the status before recording a name.

        Args:
            name: The organisation's name, as best you know it.
        """
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        answer = registry_lookup(name, recorder.company_domain)
        payload = json.dumps(answer.as_dict(), indent=1)
        for hit in answer.hits:
            if hit.source_url:
                recorder.fetched_urls.add(hit.source_url)
        if answer.url:
            payload += (
                "\n\nOpen that URL with open_page and quote the page itself; do not "
                "record a number this tool did not return."
            )
        recorder.log_step(
            step_type="tool_result",
            tool_name="lookup_registry",
            tool_input={"name": name},
            output_summary=payload[:2000],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return payload

    # ------------------------------------------------------------------ browser

    @beta_tool
    def open_page(url: str) -> str:
        """Read a page. Use this first for any URL you are not going to interact with.

        The page is fetched directly, which is fast. If what comes back is a bot
        challenge, an error, or a near-empty shell that only renders in a browser, it
        is reopened in the real browser automatically and you are told that happened.
        When that is the case the browser is left on that page, so the [ref=N] handles
        in the result can be clicked straight away.

        Args:
            url: Absolute URL, or a bare domain such as example.com.
        """
        if (stop := _budget_exhausted()) is not None:
            return stop
        started = time.monotonic()
        target = normalise_url(url)

        allowed, why = session._robots.allowed(target)
        if not allowed:
            recorder.log_step(
                step_type="tool_result",
                tool_name="open_page",
                tool_input={"url": target},
                output_summary=f"refused: {why}",
                url=target,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return (
                f"Navigation refused: {why}. Respect it and try another source."
            )

        result = http_get(target, timeout=settings.page_timeout_ms / 1000)
        escalate, reason = needs_browser(result)

        if not escalate:
            recorder.page.url = target
            recorder.page.title = ""
            recorder.page.screenshot_path = None
            recorder.visited_urls.add(target)
            recorder.fetched_urls.add(target)
            body = result.text[:_FETCH_TEXT_CAP]
            truncated = len(result.text) > _FETCH_TEXT_CAP
            text = (
                f"URL: {target}\nRead directly ({len(result.text)} characters, "
                f"{result.elapsed_ms}ms).\n\n{body}"
            )
            if truncated:
                text += (
                    "\n\n[truncated — open a more specific page if you need the rest]"
                )
            recorder.log_step(
                step_type="tool_result",
                tool_name="open_page",
                tool_input={"url": target},
                output_summary=text[:2000],
                url=target,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return text

        # The page did not come back readable, so pay for the browser.
        nav = session.goto(target)
        delivered = _deliver(
            nav.view,
            tool_name="open_page",
            tool_input={"url": target, "escalated": True, "reason": reason},
            include_screenshot=False,
            started=started,
        )
        return f"Opened in the browser because {reason}.\n\n{delivered}"


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
    def record_findings(findings: list[FindingInput]) -> str:
        """Record everything you have just observed on the page in front of you.

        Record while the page is still open, then move on. Put every claim from this
        page in a single call: one call per page, not one call per claim. Each entry
        still carries its own source URL and quote, so a page that confirms eight
        things produces eight sourced findings from one call.

        Do not save these up across pages or to the end of the run. A claim written
        from memory after you have navigated away is the thing this tool exists to
        prevent.

        Args:
            findings: Every claim observed on the current page. For a list of
                products, that is one entry per product, all in this one call.
        """
        if not findings:
            return "Nothing recorded: the findings list was empty."

        recorded: list[int] = []
        warnings: list[str] = []
        rejected: list[str] = []

        for item in findings:
            try:
                finding_id, warning = recorder.record_finding(
                    field_name=item.field,
                    value=item.value,
                    kind=item.kind,
                    evidence_url=item.evidence_url,
                    evidence_quote=item.evidence_quote,
                    confidence=item.confidence,
                    reason=item.reason,
                )
            except ValueError as exc:
                # One bad entry must not discard the good ones in the same call.
                rejected.append(f"{item.field!r}: {exc}")
                continue
            recorded.append(finding_id)
            if warning:
                warnings.append(f"{item.field!r}: {warning}")

        recorder.log_step(
            step_type="tool_result",
            tool_name="record_findings",
            tool_input={
                "count": len(findings),
                "fields": [item.field for item in findings],
            },
            output_summary=(
                f"{len(recorded)} recorded, {len(rejected)} rejected"
                + (f", {len(warnings)} warned" if warnings else "")
            ),
            url=findings[0].evidence_url,
        )

        msg = f"Recorded {len(recorded)} finding(s)."
        if warnings:
            msg += " WARNINGS: " + "; ".join(warnings)
        if rejected:
            msg += " NOT RECORDED: " + "; ".join(rejected)
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

    @beta_tool
    def note_followup(
        entity: str,
        kind: Literal["subsidiary", "parent", "investor", "competitor", "partner"],
        reason: str,
    ) -> str:
        """Flag another *organisation* worth researching in its own right.

        Use this when you find a company whose own details would materially change the
        picture -- a parent that actually owns the business, a subsidiary that holds
        the operations, a named investor, a direct competitor. Do not research it
        yourself; note it and carry on with your own question.

        Only organisations. Never use this for a person.

        Args:
            entity: The organisation's name, as the page gave it.
            kind: How it relates to the company being researched.
            reason: One line on why it is worth a closer look.
        """
        recorder.note_followup(entity=entity, kind=kind, reason=reason)
        recorder.log_step(
            step_type="tool_result",
            tool_name="note_followup",
            tool_input={"entity": entity, "kind": kind},
            output_summary=f"noted {entity!r} ({kind})",
            url=recorder.page.url,
        )
        return (
            f"Noted {entity!r} as a {kind} worth following up. "
            "Continue with your own question."
        )

    recording = [record_findings, mark_unknown, record_person, finish_research]
    if settings.depth_profile.max_children > 0:
        recording.insert(-1, note_followup)

    # A register is reached over plain HTTP, so every engine gets it. Keeping it
    # common is also what keeps the engine comparison about page access alone.
    if settings.engine == "fetch":
        # No browser tools; the model works through SERVER_TOOLS alone.
        return [lookup_registry, *recording]

    if settings.engine == "browser_toolset":
        # Anthropic's browser members instead of Scout's own, executed against the
        # same session. The SDK disables every member the toolset does not implement,
        # so the ones Scout cannot express are never offered to the model.
        from scout.browser.toolset import ScoutBrowserToolset

        assert session is not None
        return [
            ScoutBrowserToolset(session=session, recorder=recorder),
            lookup_registry,
            *recording,
        ]

    return [
        lookup_registry,
        open_page,
        browser_goto,
        browser_snapshot,
        browser_click,
        browser_type,
        browser_scroll,
        browser_back,
        browser_find_links,
        *recording,
    ]
