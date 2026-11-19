"""Second pass: turn recorded findings into a structured profile.

This runs *after* research and sees only what was recorded, never the live pages. That
is the point: the model cannot introduce a claim at synthesis time that no page
supported, because the pages are no longer available to it.
"""

from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic

from scout.agent.schemas import SynthesisResult
from scout.config import Settings

SYNTHESIS_SYSTEM = """\
You are given a set of research findings about one company. Each was recorded from a
specific page, with the source URL and a supporting quote, and is labelled as an observed
fact, an inference, or an explicit unknown.

Reorganise them into a structured profile.

Rules:

* Use only what the findings contain. Do not add industry knowledge, do not fill a gap
  from what you happen to know about the company, and do not generalise beyond the quotes.
* A field supported only by findings of kind `inference` should read as a qualified
  statement, not a flat assertion.
* Every field with no supporting finding goes in `unknowns`, left null or empty above.
* If no ideal customer profile was supplied, set `qualification` to null. Do not invent
  criteria to score against.
* Where `conflicts` lists a field, sources disagreed about it. Say so in the profile
  rather than silently choosing one value, and prefer the value carrying the stronger
  source where you must choose.
* A claim resting on one source is not wrong, but it is weaker than one several
  independent sources agree on. Let `corroboration_summary` temper how firmly the
  summary asserts things.
"""


def synthesize(
    *,
    client: Anthropic,
    settings: Settings,
    findings: list[dict],
    people: list[dict],
    objectives: str | None,
    icp: dict | None,
    research_summary: str | None,
    corroboration: dict | None = None,
    conflicts: list[str] | None = None,
) -> tuple[SynthesisResult, Any]:
    """Return the profile and the call's usage.

    The usage is returned, not discarded, because this is a second model call per
    company and leaving it out understated the reported cost of every run.
    """
    payload = {
        "research_objectives": objectives,
        "corroboration_summary": corroboration,
        "conflicts": conflicts or [],
        "ideal_customer_profile": icp,
        "agent_summary": research_summary,
        "findings": findings,
        "people": people,
    }

    response = client.messages.parse(
        model=settings.synthesis_model,
        max_tokens=16_000,
        # The instructions never change, so they are worth a breakpoint even though
        # the findings payload after them differs every time.
        system=[
            {
                "type": "text",
                "text": SYNTHESIS_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    "Here are the recorded findings. Assemble the profile.\n\n"
                    + json.dumps(payload, indent=2, default=str)
                ),
            }
        ],
        output_format=SynthesisResult,
    )
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError(
            f"synthesis returned no parsed output (stop_reason={response.stop_reason})"
        )
    return parsed, getattr(response, "usage", None)
