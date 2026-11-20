"""One row per company, with the same columns every time.

Research produced good reading and unusable data. Three runs on one company recorded
the same facts under `identifier_ods_code`, `total_headcount_2025_26_narrative`,
`operational_pressure_handover_delays_march_2026` and `registration_number (Companies
House)`. Every one of those is a sensible name for what it holds, and no two runs would
agree on them, so nothing can be sorted, filtered or written into a CRM. Enrichment is
the same columns for every company or it is prose.

Three things are needed and they are deliberately separate:

* a fixed vocabulary, which the agent is also told, so most findings arrive correct;
* an alias map, because a prompt is a request and this has to hold anyway;
* a choice of one value per column, made from the evidence rather than from order.

Nothing is discarded. A finding that matches no column is kept as an extra, and the
values not chosen are kept as alternates, in the same spirit as recording an unknown
rather than omitting the field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from scout.agent.merge import Claim, merge_findings
from scout.agent.sources import TIER_REGISTRY, tier_name


@dataclass(frozen=True)
class Column:
    """One column of the profile."""

    name: str
    description: str
    multi: bool = False
    aliases: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    core: bool = False


# The vocabulary. Order is the order of the exported row.
COLUMNS: tuple[Column, ...] = (
    Column(
        "legal_name",
        "Registered legal name, exactly as a register has it.",
        aliases=("registered_name", "company_legal_name", "entity_name", "legal_entity"),
        core=True,
    ),
    Column(
        "trading_name",
        "The name it trades under, when different from the legal name.",
        aliases=("company_name", "name", "brand", "company_name_variant", "known_as"),
        core=True,
    ),
    Column(
        "website",
        "Primary website URL.",
        aliases=("url", "homepage", "web", "domain"),
        core=True,
    ),
    Column(
        "registration_number",
        "Company number, charity number or official organisation code.",
        aliases=(
            "company_number", "registered_number", "crn", "ods_code",
            "identifier_ods_code", "organisation_code", "charity_number",
            "registry_identifier", "vat_number",
        ),
        patterns=(r"registration[_ ]?number", r"\bods\b.*code", r"company[_ ]?number"),
        core=True,
    ),
    Column(
        "legal_status",
        "Entity type: private limited company, PLC, NHS foundation trust, charity.",
        aliases=("entity_type", "company_type", "legal_form", "status", "incorporation_type"),
        patterns=(r"legal[_ ]?(status|form|basis)", r"entity[_ ]?type"),
    ),
    Column(
        "regulator",
        "The body that licenses or regulates it.",
        aliases=("regulated_by", "supervisor", "oversight"),
        patterns=(r"regulat",),
    ),
    Column(
        "headquarters_location",
        "Head office location as published.",
        aliases=("headquarters", "headquarters_address", "hq", "registered_office", "address", "locations"),
        patterns=(r"head\s?quarter", r"registered[_ ]?office"),
        core=True,
    ),
    Column(
        "country",
        "Country of the head office.",
        aliases=("hq_country", "home_country", "jurisdiction"),
    ),
    Column(
        "industry",
        "Primary industry or sector.",
        aliases=("sector", "vertical", "industry_classification", "sic", "industry_classification_code"),
        patterns=(r"industr", r"\bsic\b", r"\bnace\b"),
        core=True,
    ),
    Column(
        "business_activities",
        "What it actually does, in a sentence or two.",
        aliases=("what_they_do", "description", "overview", "business_model", "activities"),
    ),
    Column(
        "products_services",
        "Named products or services, one per entry.",
        multi=True,
        aliases=("products", "services", "offerings", "product_lines", "solutions"),
        patterns=(r"product", r"\bservices?\b"),
        core=True,
    ),
    Column(
        "employee_count",
        "Headcount or size band, as evidenced.",
        aliases=("company_size", "headcount", "staff", "employees", "size", "total_headcount", "staff_numbers"),
        patterns=(r"head\s?count", r"employee", r"staff(ing)?[_ ]?(number|count|level)?$", r"company[_ ]?size"),
        core=True,
    ),
    Column(
        "revenue",
        "Revenue, turnover or total income, with its period.",
        aliases=("turnover", "income", "annual_revenue", "total_income", "operating_income", "budget"),
        patterns=(r"revenue", r"turnover", r"\bincome\b", r"expenditure", r"budget"),
        core=True,
    ),
    Column(
        "founded",
        "Year founded, incorporated or established.",
        aliases=("founded_year", "incorporated", "established", "formation", "inception", "year_founded"),
        patterns=(r"found(ed|ing)", r"incorporat", r"establish", r"formation"),
    ),
    Column(
        "ownership",
        "Parent, group, investors or major shareholders.",
        multi=True,
        aliases=("parent", "owner", "investors", "shareholders", "group", "subsidiaries", "commissioners"),
        patterns=(r"owner", r"parent", r"investor", r"shareholder", r"subsidiar"),
    ),
    Column(
        "geographic_presence",
        "Countries or regions where it operates.",
        multi=True,
        aliases=("locations_served", "geography", "regions", "markets_served", "geographic_scope", "coverage"),
        patterns=(r"geograph", r"\bregions?\b", r"territor"),
    ),
    Column(
        "target_markets",
        "Markets or customer segments served.",
        multi=True,
        aliases=("customers", "segments", "clients", "customer_segments", "partners"),
        patterns=(r"target[_ ]?market", r"customer", r"\bclients?\b", r"partner"),
    ),
    Column(
        "technology_signals",
        "Technologies, platforms or tooling in evidence.",
        multi=True,
        aliases=("tech_stack", "technologies", "tooling", "platforms", "systems"),
        patterns=(r"technolog", r"tech[_ ]?stack"),
    ),
    Column(
        "recent_developments",
        "Recent news, funding, hiring, expansion or published pressure.",
        multi=True,
        aliases=("news", "signals", "recent_news", "developments", "buying_signal", "events"),
        patterns=(r"recent", r"signal", r"operational[_ ]?pressure", r"\bnews\b", r"announce"),
    ),
)

BY_NAME = {c.name: c for c in COLUMNS}
CORE_COLUMNS = tuple(c.name for c in COLUMNS if c.core)

_ALIAS_INDEX: dict[str, str] = {}
for _column in COLUMNS:
    _ALIAS_INDEX[_column.name] = _column.name
    for _alias in _column.aliases:
        _ALIAS_INDEX[_alias] = _column.name

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.I), c.name) for c in COLUMNS for p in c.patterns
)

# Stripped before matching: a run names a field for the year or the source it came
# from, which is bookkeeping rather than a different fact.
_NOISE = re.compile(
    r"(_|\b)(20\d\d([-_/]\d\d)?|fy\d\d|q[1-4]|per|from|as_at|latest|narrative|"
    r"corroboration|corroborated|assessment|confirmation|detail|note)(\b|_)",
    re.I,
)
_BRACKETED = re.compile(r"\s*[\(\[][^)\]]*[)\]]")


def canonical_field(raw: str) -> str | None:
    """Map a recorded field name onto a column, or None if nothing fits.

    Tried in order of how much interpretation each step takes: the name itself, then
    the name with a run's bookkeeping stripped, then a pattern. Anything still
    unmatched is an extra, not a guess.
    """
    if not raw:
        return None
    name = _BRACKETED.sub("", raw).strip().strip(":").strip()
    key = re.sub(r"[\s\-/]+", "_", name).strip("_").lower()
    if key in _ALIAS_INDEX:
        return _ALIAS_INDEX[key]

    # "leader:Chief Executive" and friends: a prefix before a colon is the real field.
    head = key.split(":", 1)[0]
    if head in _ALIAS_INDEX:
        return _ALIAS_INDEX[head]

    cleaned = _NOISE.sub("_", key).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if cleaned in _ALIAS_INDEX:
        return _ALIAS_INDEX[cleaned]

    # Patterns are written as prose, and an underscore is a word character, so
    # `\bincome\b` would never match `total_income_activities`.
    spaced = cleaned.replace("_", " ")
    for pattern, column in _PATTERNS:
        if pattern.search(spaced):
            return column
    return None


@dataclass
class Cell:
    """One column's answer, with what it rests on."""

    column: str
    value: str | None = None
    values: list[str] = dc_field(default_factory=list)
    kind: str | None = None
    sources: list[str] = dc_field(default_factory=list)
    tier: int | None = None
    corroborated: bool = False
    alternates: list[str] = dc_field(default_factory=list)
    recorded_as: list[str] = dc_field(default_factory=list)

    @property
    def filled(self) -> bool:
        return bool(self.value or self.values)

    def as_text(self) -> str:
        if self.values:
            return " | ".join(self.values)
        return self.value or ""


@dataclass
class Extra:
    """A finding that matched no column. Kept, never dropped."""

    field: str
    value: str
    sources: list[str] = dc_field(default_factory=list)


@dataclass
class Profile:
    cells: dict[str, Cell]
    extras: list[Extra]

    def coverage(self, columns: tuple[str, ...] | None = None) -> float:
        names = columns or CORE_COLUMNS
        filled = sum(1 for n in names if self.cells[n].filled)
        return filled / len(names) if names else 0.0

    def row(self) -> dict[str, str]:
        """The flat row: one column, one string."""
        return {name: self.cells[name].as_text() for name in BY_NAME}

    def missing(self, columns: tuple[str, ...] | None = None) -> list[str]:
        return [n for n in (columns or CORE_COLUMNS) if not self.cells[n].filled]


# How a claim earns the column, best first: corroborated over not, stronger source
# over weaker, fact over inference, then more evidence. Deliberately not "first
# recorded" -- order of arrival is an accident of which branch ran first.
def _rank(claim: Claim) -> tuple:
    return (
        0 if claim.corroborated else 1,
        claim.best_tier if claim.best_tier is not None else 99,
        0 if claim.kind == "fact" else 1,
        -claim.source_count,
        -len(claim.finding_ids),
        len(str(claim.value)),
    )


def build_profile(rows: list[dict], company_domain: str | None = None) -> Profile:
    """Turn a company's findings into one row of fixed columns.

    ``rows`` is what ``read_findings`` returns. Unknowns carry no value and are not
    evidence of anything, so they are skipped -- a column with no claim is already
    reported as missing.
    """
    usable = [r for r in rows if r.get("kind") != "unknown" and r.get("value") not in (None, "")]
    merged = merge_findings(usable, company_domain=company_domain)

    by_column: dict[str, list[Claim]] = {}
    extras: list[Extra] = []
    recorded_as: dict[str, set[str]] = {}

    for claim in merged.claims:
        column = canonical_field(claim.field)
        if column is None:
            extras.append(
                Extra(
                    field=claim.field,
                    value=_clean(claim.value),
                    sources=sorted(claim.hosts),
                )
            )
            continue
        by_column.setdefault(column, []).append(claim)
        recorded_as.setdefault(column, set()).add(claim.field)

    cells: dict[str, Cell] = {}
    for name, column in BY_NAME.items():
        claims = sorted(by_column.get(name, []), key=_rank)
        cell = Cell(column=name, recorded_as=sorted(recorded_as.get(name, ())))
        if claims:
            if column.multi:
                seen: set[str] = set()
                for claim in claims:
                    text = _clean(claim.value)
                    if text and text.lower() not in seen:
                        seen.add(text.lower())
                        cell.values.append(text)
                        cell.sources.extend(sorted(claim.hosts))
                best = claims[0]
            else:
                best = claims[0]
                cell.value = _clean(best.value)
                cell.alternates = [_clean(c.value) for c in claims[1:]]
                cell.sources = sorted(best.hosts)
            cell.kind = best.kind
            cell.tier = best.best_tier
            cell.corroborated = best.corroborated
            cell.sources = sorted(set(cell.sources))
        cells[name] = cell

    _derive(cells)
    return Profile(cells=cells, extras=extras)


def _derive(cells: dict[str, Cell]) -> None:
    """Fill what follows from what is already there, and say that it was derived.

    Only one rule so far, and it earns its place: a name taken from a statutory
    register *is* the legal name, whatever field the agent filed it under. Without
    this, a run that reads the register and records the result as `company_name`
    leaves the legal-name column empty while holding the answer.
    """
    legal, trading = cells["legal_name"], cells["trading_name"]
    if not legal.filled and trading.filled and trading.tier == TIER_REGISTRY:
        legal.value = trading.value
        legal.kind = trading.kind
        legal.sources = list(trading.sources)
        legal.tier = trading.tier
        legal.corroborated = trading.corroborated
        legal.recorded_as = [*trading.recorded_as, "derived from the register entry"]


def _clean(raw) -> str:
    """Values are stored as JSON, so a string arrives quoted."""
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    return " ".join(text.strip().strip('"').split())


def describe_columns(core_only: bool = False) -> str:
    """The vocabulary, for the agent's prompt."""
    lines = []
    for column in COLUMNS:
        if core_only and not column.core:
            continue
        suffix = " (one entry per item)" if column.multi else ""
        lines.append(f"- `{column.name}`: {column.description}{suffix}")
    return "\n".join(lines)


def tier_label(tier: int | None) -> str:
    return tier_name(tier)
