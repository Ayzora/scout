"""Pydantic models for the synthesised output.

Every field is required so the generated JSON schema is strict-friendly; fields that
may genuinely have no value are typed nullable rather than given a default.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CompanyProfile(BaseModel):
    """The structured profile, assembled only from findings already recorded."""

    company_name: str | None = Field(description="Registered or trading name.")
    website: str | None = Field(description="Primary website URL.")
    headquarters_location: str | None = Field(
        description="Head office location as published."
    )
    industry: str | None = Field(description="Primary industry or sector.")
    business_activities: str | None = Field(
        description="What the company actually does, in one or two sentences."
    )
    products_services: list[str] = Field(description="Named products or services.")
    company_size: str | None = Field(
        description="Headcount or size band, only where evidenced."
    )
    geographic_presence: list[str] = Field(
        description="Countries or regions where the company operates."
    )
    target_markets: list[str] = Field(description="Markets or customer segments served.")
    technology_signals: list[str] = Field(
        description="Technologies, platforms or tooling in evidence."
    )
    recent_developments: list[str] = Field(
        description="Recent news, hiring, funding or expansion signals."
    )
    unknowns: list[str] = Field(
        description="Requested fields that could not be established."
    )
    summary: str = Field(description="Two or three sentences a salesperson could use.")


class QualificationResult(BaseModel):
    score: float = Field(description="0-100 fit against the stated ICP.")
    tier: Literal["strong", "moderate", "weak", "unqualified"]
    rationale: str = Field(description="Why this score, referring to the evidence.")
    matched_criteria: list[str]
    missed_criteria: list[str]


class SynthesisResult(BaseModel):
    profile: CompanyProfile
    qualification: QualificationResult | None = Field(
        description="Null when no ideal customer profile was supplied."
    )
