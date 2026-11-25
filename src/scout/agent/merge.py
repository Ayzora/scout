"""Turn a tree of branch findings into one assessed set of claims.

Branches research independently, so the same fact arrives several times, worded
differently and sometimes contradicting itself. Grouping claims and counting sources
is arithmetic, so no model is involved and the corroboration count is a fact about
the evidence rather than a judgement. Nothing is deleted; a single-sourced claim is
flagged.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass, field as dc_field

_CJK = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_DIGIT_RUN = re.compile(r"\d+")

from scout.agent.sources import TIER_COMPANY, TIER_REGISTRY, host_of, source_tier

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
    # Japanese registers write Latin names in full-width characters, so a register
    # entry and the company's own page give the same name in two forms. Without
    # folding them, a register confirming a name reads as an unrelated claim.
    text = unicodedata.normalize("NFKC", text)
    text = _PUNCT.sub(" ", text).lower()
    return " ".join(sorted(_WS.sub(" ", text).strip().split()))


def is_refinement(a, b) -> bool:
    """Whether one value is a more precise statement of the other.

    "London HQ" and "London, UK (near Liverpool Street station)" are not rivals; one
    is simply more specific. Treating them as a conflict buries the disagreements that
    matter: every field with a short and
    a long phrasing was reported as a contradiction.

    Containment of the distinctive words, in either direction, with no clashing numbers.
    """
    if _written_without_spaces(a) or _written_without_spaces(b):
        return _is_refinement_without_words(a, b)

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


def _written_without_spaces(value) -> bool:
    text = str(value if value is not None else "")
    return len(_CJK.findall(text)) >= max(4, len(text) // 4)


def _is_refinement_without_words(a, b) -> bool:
    """The same judgement for a script with no word boundaries.

    A Japanese address is one long token, so the word-overlap test above finds no
    shared words at all and reports two spellings of one address as a contradiction.
    A postcode on one source and a prefecture on the other make one address look
    like two, and every character between them is shared.

    So: the longest run the two share, against the length of the shorter. Numbers are
    held to the same standard as everywhere else, by digit run rather than by word.
    """
    ca = _WS.sub("", _PUNCT.sub("", unicodedata.normalize("NFKC", str(a or "")))).lower()
    cb = _WS.sub("", _PUNCT.sub("", unicodedata.normalize("NFKC", str(b or "")))).lower()
    if not ca or not cb:
        return False
    short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)

    digits_short = set(_DIGIT_RUN.findall(short))
    digits_long = set(_DIGIT_RUN.findall(long_))
    if digits_short and not digits_short <= digits_long:
        return False

    match = SequenceMatcher(None, short, long_, autojunk=False).find_longest_match(
        0, len(short), 0, len(long_)
    )
    return match.size / len(short) >= REFINEMENT_OVERLAP


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


def merge_findings(rows: list[dict], company_domain: str | None = None) -> MergeResult:
    """Group findings into claims and find the disagreements.

    Each row needs ``field``, ``value``, ``kind`` and ``source_url``;
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
        # Re-derived from the URL rather than read from the row. The host lists
        # grow as real runs meet real sources -- NHS England sat in the tier-3
        # default until a run showed it confirming a trust's legal name -- and a
        # claim's standing should reflect what is known now, not what was known on
        # the day it was recorded. The stored tier stays as the record of that day.
        tier = source_tier(row.get("source_url") or "", company_domain)
        if tier is not None:
            target.tiers.add(tier)
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
