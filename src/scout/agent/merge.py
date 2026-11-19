"""Turn a tree of branch findings into one assessed set of claims.

Branches research independently, so the same fact arrives several times, sometimes
worded differently and sometimes contradicting itself. This step decides what is
corroborated, what rests on a single source, and where sources disagree.

No model is involved. Grouping claims and counting sources is arithmetic, and doing it
in Python means the assessment cannot be talked into existence -- the corroboration
count on a profile is a fact about the evidence rows, not a judgement.

Nothing is deleted. A single-sourced claim is flagged, exactly as an unestablished
field is recorded with ``mark_unknown`` rather than omitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from scout.agent.sources import TIER_COMPANY, TIER_REGISTRY, host_of

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")

# Values this close are treated as the same claim worded differently.
SAME_VALUE_OVERLAP = 0.8

# How much of a shorter claim must appear in a longer one for the longer to count as
# a more precise version of it rather than a rival to it.
REFINEMENT_OVERLAP = 0.7


def normalise_value(value) -> str:
    """A comparable form of a claim's value.

    Case, punctuation and word order vary between branches describing one fact, so
    comparison is on a sorted bag of words.
    """
    text = str(value if value is not None else "")
    text = _PUNCT.sub(" ", text).lower()
    return " ".join(sorted(_WS.sub(" ", text).strip().split()))


def is_refinement(a, b) -> bool:
    """Whether one value is a more precise statement of the other.

    "London HQ" and "London, UK (near Liverpool Street station)" are not rivals; one
    is simply more specific. Treating them as a conflict buries the disagreements that
    matter, which is what happened on the first real run: every field with a short and
    a long phrasing was reported as a contradiction.

    Containment of the distinctive words, in either direction, with no clashing numbers.
    """
    wa, wb = set(normalise_value(a).split()), set(normalise_value(b).split())
    if not wa or not wb:
        return False
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    digits_short = {w for w in short if any(c.isdigit() for c in w)}
    digits_long = {w for w in long_ if any(c.isdigit() for c in w)}
    # A figure in the narrower claim that the broader one contradicts is a real clash.
    if digits_short and not digits_short <= digits_long:
        return False
    content = {w for w in short if len(w) > 2}
    if not content:
        return False
    return len(content & long_) / len(content) >= REFINEMENT_OVERLAP


def values_agree(a, b) -> bool:
    """Whether two values are the same claim.

    Word overlap rather than equality, so "Coventry, United Kingdom" and "Coventry UK"
    agree while "Coventry" and "Manchester" do not. Deliberately strict about numbers:
    a value whose digits differ is a different claim however similar the wording, which
    is what keeps "140 people" and "1,400 people" apart.
    """
    wa, wb = set(normalise_value(a).split()), set(normalise_value(b).split())
    if not wa or not wb:
        return wa == wb
    digits_a = {w for w in wa if any(c.isdigit() for c in w)}
    digits_b = {w for w in wb if any(c.isdigit() for c in w)}
    if digits_a != digits_b:
        return False
    overlap = len(wa & wb) / min(len(wa), len(wb))
    return overlap >= SAME_VALUE_OVERLAP


@dataclass
class Claim:
    """One field's value, with every source that supports it."""

    field: str
    value: object
    kind: str
    hosts: set[str] = dc_field(default_factory=set)
    tiers: set[int] = dc_field(default_factory=set)
    finding_ids: list[int] = dc_field(default_factory=list)
    branches: set[str] = dc_field(default_factory=set)

    @property
    def source_count(self) -> int:
        """Distinct hosts, not evidence rows.

        Three pages of one site are one source. Counting rows would make a claim look
        corroborated by being mentioned repeatedly in the same place.
        """
        return len(self.hosts)

    @property
    def corroborated(self) -> bool:
        """Two independent hosts, at least one of them primary.

        The tier condition stops two aggregators -- which commonly copy the same
        original -- from corroborating each other.
        """
        return self.source_count >= 2 and bool(
            self.tiers & {TIER_REGISTRY, TIER_COMPANY}
        )

    @property
    def best_tier(self) -> int | None:
        return min(self.tiers) if self.tiers else None


@dataclass
class Conflict:
    """Two or more incompatible values recorded as fact for one field."""

    field: str
    claims: list[Claim]

    def describe(self) -> str:
        parts = [
            f"{c.value!r} ({c.source_count} source"
            f"{'s' if c.source_count != 1 else ''}, "
            f"tier {c.best_tier})"
            for c in self.claims
        ]
        return f"{self.field}: " + " vs ".join(parts)


@dataclass
class MergeResult:
    claims: list[Claim]
    conflicts: list[Conflict]

    @property
    def corroborated(self) -> list[Claim]:
        return [c for c in self.claims if c.corroborated]

    @property
    def single_sourced(self) -> list[Claim]:
        return [c for c in self.claims if not c.corroborated]

    def stats(self) -> dict[str, float | int | None]:
        facts = [c for c in self.claims if c.kind == "fact"]
        return {
            "claims": len(self.claims),
            "corroborated": len(self.corroborated),
            "single_sourced": len(self.single_sourced),
            "conflicts": len(self.conflicts),
            "corroboration_rate": (
                len([c for c in facts if c.corroborated]) / len(facts)
                if facts
                else None
            ),
        }


def merge_findings(rows: list[dict]) -> MergeResult:
    """Group findings into claims and find the disagreements.

    Each row needs ``field``, ``value``, ``kind``, ``source_url`` and ``source_tier``;
    ``finding_id`` and ``branch`` are carried through when present. A field may hold
    several genuine values at once -- a company has many products -- so agreeing values
    merge and differing ones become separate claims, which only count as a conflict
    when the field is one that can hold a single value.
    """
    by_field: dict[str, list[Claim]] = {}

    for row in rows:
        fname = row["field"]
        claims = by_field.setdefault(fname, [])
        for claim in claims:
            if claim.kind == row["kind"] and values_agree(claim.value, row["value"]):
                target = claim
                break
        else:
            target = Claim(field=fname, value=row["value"], kind=row["kind"])
            claims.append(target)

        if row.get("source_url"):
            target.hosts.add(host_of(row["source_url"]))
        if row.get("source_tier") is not None:
            target.tiers.add(row["source_tier"])
        if row.get("finding_id") is not None:
            target.finding_ids.append(row["finding_id"])
        if row.get("branch"):
            target.branches.add(row["branch"])

    all_claims = [c for claims in by_field.values() for c in claims]
    conflicts = []
    for fname, claims in by_field.items():
        if not is_single_valued(fname):
            continue
        facts = [c for c in claims if c.kind == "fact"]
        rivals = _irreconcilable(facts)
        if len(rivals) > 1:
            conflicts.append(Conflict(field=fname, claims=rivals))
    return MergeResult(claims=all_claims, conflicts=conflicts)


def _irreconcilable(facts: list[Claim]) -> list[Claim]:
    """Drop claims that merely restate another more or less precisely.

    What remains are values that genuinely cannot all be true.
    """
    kept: list[Claim] = []
    for claim in sorted(facts, key=lambda c: len(normalise_value(c.value))):
        if any(is_refinement(claim.value, other.value) for other in kept):
            continue
        kept.append(claim)
    return kept


# Fields that can only have one true answer, so two different values mean one is wrong.
# Everything else -- products, markets, developments -- legitimately holds a list, and
# flagging those as conflicts would bury the real disagreements in noise.
SINGLE_VALUED_FIELDS = frozenset(
    {
        "website",
        "headquarters_location",
        "founded",
        "legal_entity",
        "registration_number",
        "ceo",
    }
)

# Deliberately absent, learned from the first run against real findings:
#
#   company_size   the agent uses it for customers, revenue, headcount and assets
#                  alike, so several values are expected rather than contradictory
#   company_name   a trading name and a registered name are both correct
#   industry       a plain description and an SIC code are not rivals
#   revenue        a figure per year is a series, not a disagreement
#
# Flagging those produced pages of false conflicts and hid the real ones.


def is_single_valued(field_name: str) -> bool:
    return field_name in SINGLE_VALUED_FIELDS
