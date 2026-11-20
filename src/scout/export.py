"""CSV export.

One row per company in fixed columns, with the sources kept alongside, because a
profile without its sources is not what Scout promises.

The columns come from ``scout.profile`` and are the same for every company and every
run. That is the point: a run that names a field for the year it found it produces
good reading and a file nothing can sort. Findings that match no column are not lost
-- they go to ``extras``, with their sources, at the end of the row.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scout.config import Settings
from scout.db import models as m
from scout.db.session import session_scope
from scout.profile import BY_NAME, build_profile

IDENTITY_COLUMNS = ["company", "domain", "status"]

# For every profile column, the value and where it came from. Two columns per field
# rather than one, because a value whose source you have to go and look up in the
# database is not usable on its own.
PROFILE_COLUMNS = [
    part
    for name in BY_NAME
    for part in (name, f"{name}__sources")
]

ASSESSMENT_COLUMNS = [
    "core_coverage",
    "corroborated_fields",
    "missing_core_fields",
    "qualification_score",
    "qualification_tier",
    "qualification_rationale",
    "people",
    "summary",
]

RUN_COLUMNS = [
    "facts",
    "inferences",
    "unknowns",
    "evidence_coverage",
    "duration_s",
    "cost_usd",
    "extras_json",
    "findings_json",
]

COLUMNS = [*IDENTITY_COLUMNS, *PROFILE_COLUMNS, *ASSESSMENT_COLUMNS, *RUN_COLUMNS]


def _finding_rows(findings: list[m.Finding]) -> list[dict]:
    """Findings in the shape ``build_profile`` reads."""
    return [
        {
            "field": f.field,
            "value": f.value,
            "kind": f.kind,
            "source_url": f.evidence[0].source_url if f.evidence else "",
            "finding_id": f.id,
        }
        for f in findings
    ]


def _coverage(findings: list[m.Finding]) -> str:
    facts = [f for f in findings if f.kind == m.FindingKind.FACT.value]
    if not facts:
        return ""
    sourced = sum(1 for f in facts if f.evidence)
    return f"{sourced / len(facts):.2f}"


def export_run_csv(settings: Settings, run_id: int, out_path: Path) -> int:
    """Write one row per company in the run. Returns the number of rows."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0

    with session_scope(settings) as s:
        company_runs = (
            s.query(m.CompanyRun)
            .filter(m.CompanyRun.run_id == run_id)
            .order_by(m.CompanyRun.id)
            .all()
        )

        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()

            for cr in company_runs:
                findings = cr.findings
                profile = build_profile(
                    _finding_rows(findings), cr.company.domain
                )
                row: dict[str, str | float] = {
                    "company": cr.company.name or "",
                    "domain": cr.company.domain,
                    "status": cr.status,
                }
                for name, cell in profile.cells.items():
                    row[name] = cell.as_text()
                    row[f"{name}__sources"] = "; ".join(cell.sources)

                corroborated = [
                    n for n, c in profile.cells.items() if c.corroborated
                ]
                row.update(
                    {
                        "core_coverage": f"{profile.coverage():.2f}",
                        "corroborated_fields": "; ".join(corroborated),
                        "missing_core_fields": "; ".join(profile.missing()),
                        "qualification_score": (
                            cr.qualification.score if cr.qualification else ""
                        ),
                        "qualification_tier": (
                            cr.qualification.tier if cr.qualification else ""
                        ),
                        "qualification_rationale": (
                            cr.qualification.rationale or ""
                            if cr.qualification
                            else ""
                        ),
                        "people": "; ".join(
                            f"{p.name} ({p.role})" if p.role else p.name
                            for p in cr.people
                        ),
                        "summary": cr.summary or "",
                        "facts": sum(
                            1 for f in findings if f.kind == m.FindingKind.FACT.value
                        ),
                        "inferences": sum(
                            1
                            for f in findings
                            if f.kind == m.FindingKind.INFERENCE.value
                        ),
                        "unknowns": sum(
                            1
                            for f in findings
                            if f.kind == m.FindingKind.UNKNOWN.value
                        ),
                        "evidence_coverage": _coverage(findings),
                        "duration_s": (
                            f"{cr.duration_s:.1f}" if cr.duration_s else ""
                        ),
                        "cost_usd": (
                            f"{cr.cost_usd:.4f}" if cr.cost_usd is not None else ""
                        ),
                        # Findings that matched no column. Kept so a vocabulary gap
                        # is visible in the output rather than only in the database.
                        "extras_json": json.dumps(
                            [
                                {"field": e.field, "value": e.value,
                                 "sources": e.sources}
                                for e in profile.extras
                            ],
                            default=str,
                        ),
                        "findings_json": json.dumps(
                            [
                                {
                                    "field": f.field,
                                    "value": f.value,
                                    "kind": f.kind,
                                    "sources": [e.source_url for e in f.evidence],
                                }
                                for f in findings
                            ],
                            default=str,
                        ),
                    }
                )
                writer.writerow(row)
                rows += 1
    return rows
