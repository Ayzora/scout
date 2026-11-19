"""Export output, and the loop helpers that do not need the API."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scout.agent.loop import (
    CompanyResult,
    Usage,
    _harvest_server_tool_urls,
    _persist_company_run,
    create_run,
    ensure_company,
    finish_run,
)
from scout.agent.pricing import estimate_cost_usd
from scout.agent.recorder import Recorder
from scout.agent.schemas import CompanyProfile, QualificationResult, SynthesisResult
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope
from scout.export import export_run_csv


@pytest.fixture
def settings(tmp_path):
    s = replace(load_settings(), data_dir=tmp_path)
    reset_engine()
    init_db(s)
    yield s
    reset_engine()


# ------------------------------------------------------------------ usage maths


def test_usage_accumulates_across_turns():
    usage = Usage()
    usage.add(
        SimpleNamespace(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=5,
            cache_creation_input_tokens=7,
        )
    )
    usage.add(
        SimpleNamespace(
            input_tokens=50,
            output_tokens=10,
            cache_read_input_tokens=1,
            cache_creation_input_tokens=0,
        )
    )
    usage.add(None)  # a turn with no usage must not count or crash
    assert usage.as_dict() == {
        "input_tokens": 150,
        "output_tokens": 30,
        "cache_read_tokens": 6,
        "cache_write_tokens": 7,
        "web_searches": 0,
        "turns": 2,
    }


def test_web_searches_are_counted_and_priced():
    """Web search is billed per request, so a token-only tally would miss it."""
    usage = Usage()
    for _ in range(3):
        usage.add(
            SimpleNamespace(
                input_tokens=0,
                output_tokens=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                server_tool_use=SimpleNamespace(web_search_requests=2),
            )
        )
    assert usage.web_searches == 6
    # Six searches at $10 per 1,000 and no tokens at all.
    assert estimate_cost_usd(model="claude-opus-5-5", **usage.billable()) == 0.06


def test_merge_totals_research_and_synthesis():
    """Synthesis is a second billed call; its usage must reach the total."""
    research = Usage(input_tokens=100, output_tokens=10, turns=4, web_searches=1)
    synthesis = Usage(input_tokens=20, output_tokens=5, turns=1)
    total = research.merge(synthesis)
    assert total.input_tokens == 120
    assert total.output_tokens == 15
    assert total.turns == 5
    assert total.web_searches == 1
    # The originals are untouched, so each phase can still be priced separately.
    assert research.input_tokens == 100


# -------------------------------------------------- server-tool URL harvesting


def test_harvest_collects_search_and_fetch_urls(settings):
    recorder = Recorder(settings=settings, company_run_id=1)
    content = [
        SimpleNamespace(type="text", text="hello"),
        SimpleNamespace(
            type="web_search_tool_result",
            content=[
                SimpleNamespace(url="https://a.example/1"),
                SimpleNamespace(url="https://b.example/2"),
            ],
        ),
        SimpleNamespace(
            type="web_fetch_tool_result",
            content=SimpleNamespace(url="https://c.example/3"),
        ),
    ]
    _harvest_server_tool_urls(content, recorder)
    assert recorder.fetched_urls == {
        "https://a.example/1",
        "https://b.example/2",
        "https://c.example/3",
    }


def test_harvest_survives_a_server_tool_error_block(settings):
    """An errored server tool returns an object, not a list. Must not crash."""
    recorder = Recorder(settings=settings, company_run_id=1)
    content = [
        SimpleNamespace(
            type="web_search_tool_result",
            content=SimpleNamespace(error_code="max_uses_exceeded"),
        )
    ]
    _harvest_server_tool_urls(content, recorder)
    assert recorder.fetched_urls == set()


def test_harvest_handles_no_content(settings):
    recorder = Recorder(settings=settings, company_run_id=1)
    _harvest_server_tool_urls(None, recorder)
    assert recorder.fetched_urls == set()


def test_harvested_url_satisfies_the_provenance_guard(settings):
    """A page found via web_search should not be flagged as invented."""
    with session_scope(settings) as s:
        c = m.Company(domain="x.example")
        r = m.Run(model="t", effort="high")
        s.add_all([c, r])
        s.flush()
        cr = m.CompanyRun(run_id=r.id, company_id=c.id)
        s.add(cr)
        s.flush()
        cr_id = cr.id

    recorder = Recorder(settings=settings, company_run_id=cr_id)
    _, warning = recorder.record_finding(
        field_name="industry", value="Mining", kind="fact",
        evidence_url="https://news.example/story", evidence_quote="a mining firm",
    )
    assert warning is not None  # never seen it

    recorder.fetched_urls.add("https://news.example/story")
    _, warning2 = recorder.record_finding(
        field_name="industry", value="Mining", kind="fact",
        evidence_url="https://news.example/story", evidence_quote="a mining firm",
    )
    assert warning2 is None  # now it is a page the session actually read


# ----------------------------------------------------------------- persistence


def test_company_run_metrics_and_qualification_are_persisted(settings):
    run_id = create_run(
        settings, label="t", objectives="o", requested_fields=["industry"],
        icp={"size": "50-500"},
    )
    company_id = ensure_company(settings, "acme.example", "Acme")
    with session_scope(settings) as s:
        cr = m.CompanyRun(run_id=run_id, company_id=company_id)
        s.add(cr)
        s.flush()
        cr_id = cr.id

    synthesis = SynthesisResult(
        profile=CompanyProfile(
            company_name="Acme", website="https://acme.example",
            headquarters_location="Leeds, UK", industry="Manufacturing",
            business_activities="Makes widgets.", products_services=["Widget A"],
            company_size="120 staff", geographic_presence=["UK"],
            target_markets=["Automotive"], technology_signals=["SAP"],
            recent_developments=["Hiring a CTO"], unknowns=["revenue"],
            summary="Acme makes widgets in Leeds.",
        ),
        qualification=QualificationResult(
            score=72.5, tier="moderate", rationale="Right size, wrong sector.",
            matched_criteria=["size"], missed_criteria=["sector"],
        ),
    )
    result = CompanyResult(
        company_run_id=cr_id, domain="acme.example", status="done",
        stats={"steps": 11, "facts": 6}, usage={
            "input_tokens": 1000, "output_tokens": 200,
            "cache_read_tokens": 50, "cache_write_tokens": 10,
        },
        cost_usd=0.1234, duration_s=42.5, synthesis=synthesis,
    )
    _persist_company_run(settings, result)

    with session_scope(settings) as s:
        cr = s.get(m.CompanyRun, cr_id)
        assert cr.status == "done"
        assert cr.input_tokens == 1000
        assert cr.cache_write_tokens == 10
        assert cr.cost_usd == pytest.approx(0.1234)
        assert cr.duration_s == pytest.approx(42.5)
        assert cr.step_count == 11
        assert cr.summary == "Acme makes widgets in Leeds."
        assert cr.qualification.tier == "moderate"
        assert cr.qualification.icp_match["matched"] == ["size"]


def test_ensure_company_is_idempotent_and_backfills_name(settings):
    first = ensure_company(settings, "dup.example", None)
    second = ensure_company(settings, "dup.example", "Dup Ltd")
    assert first == second
    with session_scope(settings) as s:
        assert s.query(m.Company).count() == 1
        assert s.get(m.Company, first).name == "Dup Ltd"


def test_finish_run_marks_failed_only_when_everything_failed(settings):
    run_id = create_run(
        settings, label=None, objectives=None, requested_fields=None, icp=None
    )
    cid = ensure_company(settings, "a.example", None)
    cid2 = ensure_company(settings, "b.example", None)
    with session_scope(settings) as s:
        s.add(m.CompanyRun(run_id=run_id, company_id=cid, status="failed"))
        s.add(m.CompanyRun(run_id=run_id, company_id=cid2, status="done"))
    finish_run(settings, run_id)
    with session_scope(settings) as s:
        assert s.get(m.Run, run_id).status == "done"

    run2 = create_run(
        settings, label=None, objectives=None, requested_fields=None, icp=None
    )
    with session_scope(settings) as s:
        s.add(m.CompanyRun(run_id=run2, company_id=cid, status="failed"))
    finish_run(settings, run2)
    with session_scope(settings) as s:
        assert s.get(m.Run, run2).status == "failed"


# ---------------------------------------------------------------------- export


def test_csv_export_keeps_sources_with_each_claim(settings, tmp_path):
    run_id = create_run(
        settings, label="t", objectives=None, requested_fields=None, icp=None
    )
    cid = ensure_company(settings, "acme.example", "Acme")
    with session_scope(settings) as s:
        cr = m.CompanyRun(
            run_id=run_id, company_id=cid, status="done",
            summary="Acme makes widgets.", duration_s=30.0, cost_usd=0.05,
        )
        s.add(cr)
        s.flush()
        fact = m.Finding(
            company_run_id=cr.id, field="industry", value="Manufacturing", kind="fact"
        )
        s.add(fact)
        s.flush()
        s.add(
            m.Evidence(
                finding_id=fact.id, source_url="https://acme.example/about",
                quote="We manufacture widgets",
            )
        )
        s.add(
            m.Finding(
                company_run_id=cr.id, field="revenue", value=None, kind="unknown",
                reason="not published",
            )
        )
        person = m.Person(company_run_id=cr.id, name="Dana Hall", role="CEO")
        s.add(person)
        s.flush()
        s.add(m.Evidence(person_id=person.id, source_url="https://acme.example/team"))

    out = tmp_path / "results.csv"
    rows = export_run_csv(settings, run_id, out)
    assert rows == 1

    with out.open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["company"] == "Acme"
    assert row["domain"] == "acme.example"
    assert row["facts"] == "1"
    assert row["unknowns"] == "1"
    assert row["evidence_coverage"] == "1.00"
    assert row["cost_usd"] == "0.0500"
    assert row["people"] == "Dana Hall (CEO)"

    findings = json.loads(row["findings_json"])
    industry = next(f for f in findings if f["field"] == "industry")
    assert industry["sources"] == ["https://acme.example/about"]


def test_export_reports_partial_coverage(settings, tmp_path):
    run_id = create_run(
        settings, label=None, objectives=None, requested_fields=None, icp=None
    )
    cid = ensure_company(settings, "partial.example", None)
    with session_scope(settings) as s:
        cr = m.CompanyRun(run_id=run_id, company_id=cid, status="done")
        s.add(cr)
        s.flush()
        sourced = m.Finding(
            company_run_id=cr.id, field="a", value="1", kind="fact"
        )
        s.add(sourced)
        s.add(m.Finding(company_run_id=cr.id, field="b", value="2", kind="fact"))
        s.flush()
        s.add(m.Evidence(finding_id=sourced.id, source_url="https://x.example"))

    out = tmp_path / "partial.csv"
    export_run_csv(settings, run_id, out)
    with out.open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["evidence_coverage"] == "0.50"
