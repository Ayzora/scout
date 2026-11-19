"""Writes findings, people, evidence and the agent trace to the database.

Separated from the tool definitions so the persistence rules can be tested without
an API key or a browser.

One guard lives here that is worth naming: ``record_finding`` checks the claimed
``evidence_url`` against the pages the browser actually opened. A model that writes a
plausible-looking URL it never visited is the main failure mode for evidence-backed
extraction, and this turns it from an invisible problem into a warning the agent sees
and a flag the evaluation harness can count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from urllib.parse import urlparse

from scout.config import Settings
from scout.db import models as m
from scout.db.session import session_scope


@dataclass
class PageContext:
    """Where the browser is right now, so evidence can be tied to it."""

    url: str = ""
    title: str = ""
    screenshot_path: str | None = None


@dataclass
class Recorder:
    settings: Settings
    company_run_id: int

    page: PageContext = field(default_factory=PageContext)
    visited_urls: set[str] = field(default_factory=set)
    fetched_urls: set[str] = field(default_factory=set)
    _seq: int = 0
    unsourced_claims: int = 0
    finished: bool = False
    final_summary: str | None = None

    # ------------------------------------------------------------------ trace

    def log_step(
        self,
        *,
        step_type: str,
        tool_name: str | None = None,
        tool_input: dict | None = None,
        output_summary: str | None = None,
        url: str | None = None,
        screenshot_path: str | None = None,
        duration_ms: int | None = None,
    ) -> int:
        self._seq += 1
        with session_scope(self.settings) as s:
            s.add(
                m.Step(
                    company_run_id=self.company_run_id,
                    seq=self._seq,
                    type=step_type,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    output_summary=(output_summary or "")[:4000] or None,
                    url=url,
                    screenshot_path=screenshot_path,
                    duration_ms=duration_ms,
                )
            )
        return self._seq

    @property
    def step_count(self) -> int:
        return self._seq

    # --------------------------------------------------------------- evidence

    def _provenance_warning(self, evidence_url: str) -> str | None:
        """Flag an evidence URL the agent never actually opened."""
        if not evidence_url:
            return "no evidence_url was supplied"
        if evidence_url in self.visited_urls or evidence_url in self.fetched_urls:
            return None
        host = urlparse(evidence_url).netloc
        known_hosts = {
            urlparse(u).netloc for u in (self.visited_urls | self.fetched_urls)
        }
        if host and host in known_hosts:
            return None
        self.unsourced_claims += 1
        return (
            f"evidence_url {evidence_url!r} is not a page this session opened. "
            "If you read it via search results, open it before citing it."
        )

    def _screenshot_for(self, evidence_url: str) -> str | None:
        """Attach the current screenshot only if the evidence is the open page."""
        if evidence_url and evidence_url == self.page.url:
            return self.page.screenshot_path
        return None

    # --------------------------------------------------------------- findings

    def record_finding(
        self,
        *,
        field_name: str,
        value: str,
        kind: str,
        evidence_url: str,
        evidence_quote: str | None,
        confidence: float | None = None,
        reason: str | None = None,
    ) -> tuple[int, str | None]:
        if kind not in (m.FindingKind.FACT.value, m.FindingKind.INFERENCE.value):
            raise ValueError(
                f"kind must be 'fact' or 'inference' (use mark_unknown for unknowns), "
                f"got {kind!r}"
            )
        # Checked here, not left to the table's CHECK constraint: that raises
        # IntegrityError, which aborts the run instead of giving the model something
        # it can correct.
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"confidence must be between 0.0 and 1.0, got {confidence!r}"
            )
        warning = self._provenance_warning(evidence_url)

        with session_scope(self.settings) as s:
            finding = m.Finding(
                company_run_id=self.company_run_id,
                field=field_name,
                value=value,
                kind=kind,
                confidence=confidence,
                reason=reason,
            )
            s.add(finding)
            s.flush()
            s.add(
                m.Evidence(
                    finding_id=finding.id,
                    source_url=evidence_url,
                    page_title=self.page.title or None,
                    quote=evidence_quote,
                    screenshot_path=self._screenshot_for(evidence_url),
                )
            )
            return finding.id, warning

    def mark_unknown(self, *, field_name: str, reason: str) -> int:
        with session_scope(self.settings) as s:
            finding = m.Finding(
                company_run_id=self.company_run_id,
                field=field_name,
                value=None,
                kind=m.FindingKind.UNKNOWN.value,
                reason=reason,
            )
            s.add(finding)
            s.flush()
            return finding.id

    def record_person(
        self,
        *,
        name: str,
        role: str | None,
        seniority: str | None,
        contact: dict | None,
        evidence_url: str,
        evidence_quote: str | None,
    ) -> tuple[int, str | None]:
        warning = self._provenance_warning(evidence_url)
        with session_scope(self.settings) as s:
            person = m.Person(
                company_run_id=self.company_run_id,
                name=name,
                role=role,
                seniority=seniority,
                contact=contact or None,
            )
            s.add(person)
            s.flush()
            s.add(
                m.Evidence(
                    person_id=person.id,
                    source_url=evidence_url,
                    page_title=self.page.title or None,
                    quote=evidence_quote,
                    screenshot_path=self._screenshot_for(evidence_url),
                )
            )
            return person.id, warning

    # ------------------------------------------------------------------ reads

    def snapshot_of_findings(self) -> list[dict]:
        """Everything recorded so far, with provenance. Input to synthesis."""
        with session_scope(self.settings) as s:
            findings = (
                s.query(m.Finding)
                .filter(m.Finding.company_run_id == self.company_run_id)
                .order_by(m.Finding.id)
                .all()
            )
            out = []
            for f in findings:
                out.append(
                    {
                        "field": f.field,
                        "value": f.value,
                        "kind": f.kind,
                        "confidence": f.confidence,
                        "reason": f.reason,
                        "sources": [
                            {"url": e.source_url, "quote": e.quote}
                            for e in f.evidence
                        ],
                    }
                )
            return out

    def snapshot_of_people(self) -> list[dict]:
        with session_scope(self.settings) as s:
            people = (
                s.query(m.Person)
                .filter(m.Person.company_run_id == self.company_run_id)
                .order_by(m.Person.id)
                .all()
            )
            return [
                {
                    "name": p.name,
                    "role": p.role,
                    "seniority": p.seniority,
                    "contact": p.contact,
                    "sources": [
                        {"url": e.source_url, "quote": e.quote} for e in p.evidence
                    ],
                }
                for p in people
            ]

    def stats(self) -> dict:
        with session_scope(self.settings) as s:
            findings = (
                s.query(m.Finding)
                .filter(m.Finding.company_run_id == self.company_run_id)
                .all()
            )
            facts = [f for f in findings if f.kind == m.FindingKind.FACT.value]
            sourced = [f for f in facts if f.evidence]
            return {
                "findings": len(findings),
                "facts": len(facts),
                "inferences": sum(
                    1 for f in findings if f.kind == m.FindingKind.INFERENCE.value
                ),
                "unknowns": sum(
                    1 for f in findings if f.kind == m.FindingKind.UNKNOWN.value
                ),
                "evidence_coverage": (len(sourced) / len(facts)) if facts else None,
                "unsourced_claim_warnings": self.unsourced_claims,
                "steps": self._seq,
            }
