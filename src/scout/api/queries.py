"""Read the database into the shapes the dashboard asks for.

Kept apart from the HTTP layer so each query can be tested without a server, and so
the dashboard cannot grow its own idea of what a finding is. Everything here is a
plain dict: the API serialises, it does not interpret.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func

from scout.config import Settings
from scout.db import models as m
from scout.db.session import session_scope
from scout.profile import build_profile, tier_label


def _finding_rows(findings: list[m.Finding]) -> list[dict]:
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


def list_runs(settings: Settings, limit: int = 50) -> list[dict]:
    with session_scope(settings) as s:
        runs = (
            s.query(m.Run).order_by(m.Run.id.desc()).limit(limit).all()
        )
        out = []
        for run in runs:
            companies = run.company_runs
            done = [c for c in companies if c.status == m.Status.DONE.value]
            cost = sum(c.cost_usd or 0 for c in companies)
            out.append(
                {
                    "id": run.id,
                    "label": run.label,
                    "status": run.status,
                    "objectives": run.objectives,
                    "icp": run.icp,
                    "model": run.model,
                    "engine": run.engine,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                    "finished_at": (
                        run.finished_at.isoformat() if run.finished_at else None
                    ),
                    "companies": len(companies),
                    "companies_done": len(done),
                    "companies_failed": len(
                        [c for c in companies if c.status == m.Status.FAILED.value]
                    ),
                    "cost_usd": round(cost, 4) if cost else 0.0,
                }
            )
        return out


def get_run(settings: Settings, run_id: int) -> dict | None:
    with session_scope(settings) as s:
        run = s.get(m.Run, run_id)
        if run is None:
            return None
        companies = []
        for cr in sorted(run.company_runs, key=lambda c: c.id):
            facts = [f for f in cr.findings if f.kind == m.FindingKind.FACT.value]
            companies.append(
                {
                    "id": cr.id,
                    "domain": cr.company.domain,
                    "name": cr.company.name,
                    "status": cr.status,
                    "facts": len(facts),
                    "unknowns": len(
                        [
                            f
                            for f in cr.findings
                            if f.kind == m.FindingKind.UNKNOWN.value
                        ]
                    ),
                    "people": len(cr.people),
                    "duration_s": cr.duration_s,
                    "cost_usd": cr.cost_usd,
                    "summary": cr.summary,
                    "error": cr.error,
                    "score": cr.qualification.score if cr.qualification else None,
                    "tier": cr.qualification.tier if cr.qualification else None,
                }
            )
        return {
            "id": run.id,
            "label": run.label,
            "status": run.status,
            "objectives": run.objectives,
            "icp": run.icp,
            "requested_fields": run.requested_fields,
            "model": run.model,
            "engine": run.engine,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "companies": companies,
        }


def get_company_run(settings: Settings, company_run_id: int) -> dict | None:
    from scout.agent.merge import merge_findings

    with session_scope(settings) as s:
        cr = s.get(m.CompanyRun, company_run_id)
        if cr is None:
            return None

        rows = _finding_rows(cr.findings)
        profile = build_profile(rows, cr.company.domain)
        merged = merge_findings(
            [r for r in rows if r["kind"] != m.FindingKind.UNKNOWN.value],
            company_domain=cr.company.domain,
        )

        branches = [
            {
                "id": b.id,
                "parent_id": b.parent_branch_id,
                "name": b.name,
                "question": b.question,
                "depth": b.depth,
                "status": b.status,
                "cost_usd": b.cost_usd,
                "duration_s": b.duration_s,
                "web_searches": b.web_searches,
                "summary": b.summary,
                "error": b.error,
            }
            for b in sorted(
                s.query(m.Branch)
                .filter(m.Branch.company_run_id == cr.id)
                .all(),
                key=lambda b: b.id,
            )
        ]

        return {
            "id": cr.id,
            "run_id": cr.run_id,
            "domain": cr.company.domain,
            "name": cr.company.name,
            "status": cr.status,
            "summary": cr.summary,
            "error": cr.error,
            "duration_s": cr.duration_s,
            "cost_usd": cr.cost_usd,
            "tokens": {
                "input": cr.input_tokens,
                "output": cr.output_tokens,
                "cache_read": cr.cache_read_tokens,
                "cache_write": cr.cache_write_tokens,
            },
            "profile": [
                {
                    "field": name,
                    "value": cell.as_text(),
                    "values": cell.values,
                    "kind": cell.kind,
                    "sources": cell.sources,
                    "tier": cell.tier,
                    "tier_label": tier_label(cell.tier),
                    "corroborated": cell.corroborated,
                    "alternates": cell.alternates,
                    "recorded_as": cell.recorded_as,
                    "filled": cell.filled,
                }
                for name, cell in profile.cells.items()
            ],
            "coverage": profile.coverage(),
            "missing": profile.missing(),
            "extras": [
                {"field": e.field, "value": e.value, "sources": e.sources}
                for e in profile.extras
            ],
            "assessment": {
                **merged.stats(),
                "conflicts": [
                    {
                        "field": c.field,
                        "values": [
                            {"value": str(cl.value), "sources": sorted(cl.hosts)}
                            for cl in c.claims
                        ],
                    }
                    for c in merged.conflicts
                ],
            },
            "qualification": (
                {
                    "score": cr.qualification.score,
                    "tier": cr.qualification.tier,
                    "rationale": cr.qualification.rationale,
                    "matched": (cr.qualification.icp_match or {}).get("matched", []),
                    "missed": (cr.qualification.icp_match or {}).get("missed", []),
                }
                if cr.qualification
                else None
            ),
            "people": [
                {
                    "name": p.name,
                    "role": p.role,
                    "contact": p.contact,
                    "kind": p.kind,
                    "sources": [e.source_url for e in p.evidence],
                }
                for p in cr.people
            ],
            "findings": [
                {
                    "id": f.id,
                    "field": f.field,
                    "value": f.value,
                    "kind": f.kind,
                    "confidence": f.confidence,
                    "reason": f.reason,
                    "branch_id": f.branch_id,
                    "evidence": [
                        {
                            "url": e.source_url,
                            "quote": e.quote,
                            "tier": e.source_tier,
                            "tier_label": tier_label(e.source_tier),
                        }
                        for e in f.evidence
                    ],
                }
                for f in cr.findings
            ],
            "branches": branches,
        }


def get_steps(settings: Settings, company_run_id: int) -> list[dict]:
    """The agent's trace: what it did, in order, and how long each took."""
    with session_scope(settings) as s:
        steps = (
            s.query(m.Step)
            .filter(m.Step.company_run_id == company_run_id)
            .order_by(m.Step.seq)
            .all()
        )
        return [
            {
                "seq": st.seq,
                "tool": st.tool_name,
                "input": st.tool_input,
                "output": st.output_summary,
                "url": st.url,
                "screenshot": st.screenshot_path,
                "duration_ms": st.duration_ms,
                "branch_id": st.branch_id,
                # The escalation decision, which is the interesting part of a trace:
                # most pages are read directly and only some need a browser.
                "escalated": bool((st.tool_input or {}).get("escalated"))
                if isinstance(st.tool_input, dict)
                else False,
                "reason": (st.tool_input or {}).get("reason")
                if isinstance(st.tool_input, dict)
                else None,
            }
            for st in steps
        ]


def totals(settings: Settings) -> dict[str, Any]:
    with session_scope(settings) as s:
        return {
            "runs": s.query(func.count(m.Run.id)).scalar() or 0,
            "companies": s.query(func.count(func.distinct(m.Company.id))).scalar() or 0,
            "findings": s.query(func.count(m.Finding.id)).scalar() or 0,
            "people": s.query(func.count(m.Person.id)).scalar() or 0,
            "spend_usd": round(
                s.query(func.sum(m.CompanyRun.cost_usd)).scalar() or 0.0, 4
            ),
        }
