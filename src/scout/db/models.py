"""SQLAlchemy models.

Design notes:

* ``Finding`` is the unit of extracted information, and every one carries a ``kind``:
  an observed fact, an inference, or an explicit unknown. Nothing is stored without
  that label, so the spec's "do not present guesses as verified facts" rule is enforced
  by the schema rather than by prompt discipline alone.
* ``Evidence`` is a separate table, not a column, because one claim can rest on several
  sources. A finding of kind ``fact`` with no evidence rows is a defect, and the
  evaluation harness queries for exactly that.
* ``Step`` is the ordered agent trace. It powers the live dashboard, the demo recording,
  and after-the-fact debugging of why the agent went where it did.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class FindingKind(str, enum.Enum):
    FACT = "fact"
    INFERENCE = "inference"
    UNKNOWN = "unknown"


class Status(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"  # site refused access; recorded, not evaded


class StepType(str, enum.Enum):
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    MESSAGE = "message"
    ERROR = "error"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Company(Base, TimestampMixin):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(512))
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    country: Mapped[str | None] = mapped_column(String(64))

    company_runs: Mapped[list["CompanyRun"]] = relationship(back_populates="company")

    __table_args__ = (UniqueConstraint("domain", name="uq_companies_domain"),)


class Run(Base, TimestampMixin):
    """One batch. Holds the research configuration the whole batch shares."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=Status.PENDING.value)

    objectives: Mapped[str | None] = mapped_column(Text)
    requested_fields: Mapped[list[str] | None] = mapped_column(JSON)
    icp: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    model: Mapped[str] = mapped_column(String(128))
    effort: Mapped[str] = mapped_column(String(16))

    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company_runs: Mapped[list["CompanyRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class CompanyRun(Base, TimestampMixin):
    """One company researched within one batch. Carries the cost and timing metrics."""

    __tablename__ = "company_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)

    status: Mapped[str] = mapped_column(String(32), default=Status.PENDING.value)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[float | None] = mapped_column(Float)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)

    step_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    run: Mapped["Run"] = relationship(back_populates="company_runs")
    company: Mapped["Company"] = relationship(back_populates="company_runs")
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="company_run", cascade="all, delete-orphan"
    )
    people: Mapped[list["Person"]] = relationship(
        back_populates="company_run", cascade="all, delete-orphan"
    )
    steps: Mapped[list["Step"]] = relationship(
        back_populates="company_run", cascade="all, delete-orphan"
    )
    qualification: Mapped["Qualification | None"] = relationship(
        back_populates="company_run", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("run_id", "company_id", name="uq_company_run"),
    )


class Finding(Base, TimestampMixin):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_run_id: Mapped[int] = mapped_column(
        ForeignKey("company_runs.id"), index=True
    )

    field: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[Any | None] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)  # why unknown, or how inferred

    company_run: Mapped["CompanyRun"] = relationship(back_populates="findings")
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('fact', 'inference', 'unknown')", name="ck_findings_kind"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="ck_findings_confidence",
        ),
        Index("ix_findings_run_field", "company_run_id", "field"),
    )


class Person(Base, TimestampMixin):
    """A decision-maker. Only details the company itself publishes are stored."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_run_id: Mapped[int] = mapped_column(
        ForeignKey("company_runs.id"), index=True
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str | None] = mapped_column(String(255))
    seniority: Mapped[str | None] = mapped_column(String(64))
    # Only ever populated from a page the company publishes. Never inferred or
    # pattern-guessed; see docs/data-protection.md.
    contact: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(16), default=FindingKind.FACT.value)

    company_run: Mapped["CompanyRun"] = relationship(back_populates="people")
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="person", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('fact', 'inference', 'unknown')", name="ck_people_kind"
        ),
    )


class Evidence(Base, TimestampMixin):
    """Provenance for exactly one finding or one person."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("findings.id"), index=True
    )
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), index=True)

    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_title: Mapped[str | None] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    finding: Mapped["Finding | None"] = relationship(back_populates="evidence")
    person: Mapped["Person | None"] = relationship(back_populates="evidence")

    __table_args__ = (
        CheckConstraint(
            "(finding_id IS NOT NULL AND person_id IS NULL) OR "
            "(finding_id IS NULL AND person_id IS NOT NULL)",
            name="ck_evidence_one_subject",
        ),
    )


class Step(Base, TimestampMixin):
    """Ordered trace of what the agent did. Append-only."""

    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_run_id: Mapped[int] = mapped_column(
        ForeignKey("company_runs.id"), index=True
    )

    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(128))
    tool_input: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    screenshot_path: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    company_run: Mapped["CompanyRun"] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("company_run_id", "seq", name="uq_step_seq"),
    )


class Qualification(Base, TimestampMixin):
    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_run_id: Mapped[int] = mapped_column(
        ForeignKey("company_runs.id"), unique=True, index=True
    )

    score: Mapped[float | None] = mapped_column(Float)
    tier: Mapped[str | None] = mapped_column(String(32))
    rationale: Mapped[str | None] = mapped_column(Text)
    icp_match: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    company_run: Mapped["CompanyRun"] = relationship(back_populates="qualification")


class Review(Base, TimestampMixin):
    """Human verdict on a generated finding (spec section 5: human review)."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"), index=True)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    corrected_value: Mapped[Any | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('approved', 'rejected', 'corrected')",
            name="ck_reviews_verdict",
        ),
    )
