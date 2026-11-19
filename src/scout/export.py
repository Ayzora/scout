"""CSV export.

One row per company, with the evidence URLs kept alongside each claim, because a
profile without its sources is not what Scout promises.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from scout.config import Settings
from scout.db import models as m
from scout.db.session import session_scope

COLUMNS = [
    "company",
    "domain",
    "status",
    "summary",
    "qualification_score",
    "qualification_tier",
    "facts",
    "inferences",
    "unknowns",
    "evidence_coverage",
    "duration_s",
    "cost_usd",
    "people",
    "findings_json",
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
                writer.writerow(
                    {
                        "company": cr.company.name or "",
                        "domain": cr.company.domain,
                        "status": cr.status,
                        "summary": cr.summary or "",
                        "qualification_score": (
                            cr.qualification.score if cr.qualification else ""
                        ),
                        "qualification_tier": (
                            cr.qualification.tier if cr.qualification else ""
                        ),
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
                        "people": "; ".join(
                            f"{p.name} ({p.role})" if p.role else p.name
                            for p in cr.people
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
                rows += 1
    return rows
