"""The local API the dashboard reads.

Read endpoints are tested against a database built in the test, so the shapes the
page depends on cannot drift without something failing. The one endpoint that spends
money is tested only for its refusals -- that it will not start a run without a goal,
without a key, or while another is already going.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from scout.api.app import create_app
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = replace(load_settings(), data_dir=tmp_path)
    reset_engine()
    init_db(settings)

    with session_scope(settings) as s:
        company = m.Company(domain="asiagate.co.uk", name="AsiaGate")
        run = m.Run(
            label="november", model="claude-haiku-5-5", effort="medium",
            engine="browser", status=m.Status.DONE.value,
            objectives="who runs it", icp={"sector": "UK business"},
        )
        s.add_all([company, run])
        s.flush()
        cr = m.CompanyRun(
            run_id=run.id, company_id=company.id, status=m.Status.DONE.value,
            cost_usd=0.0241, duration_s=129.0, summary="A UK sourcing agent.",
        )
        s.add(cr)
        s.flush()
        branch = m.Branch(
            company_run_id=cr.id, name="identity", depth=1,
            status=m.Status.DONE.value, question="What is the legal entity?",
            cost_usd=0.01, web_searches=0,
        )
        s.add(branch)
        s.flush()
        for field, value, url in (
            ("legal_name", '"ASIAGATE LTD"',
             "https://find-and-update.company-information.service.gov.uk/company/1"),
            ("registration_number", '"14834017"',
             "https://find-and-update.company-information.service.gov.uk/company/1"),
            ("identifier_ods_code", '"ignored"', "https://asiagate.co.uk/about"),
        ):
            f = m.Finding(
                company_run_id=cr.id, field=field, value=value, kind="fact",
                branch_id=branch.id,
            )
            s.add(f)
            s.flush()
            s.add(m.Evidence(finding_id=f.id, source_url=url, quote="q", source_tier=1))
        s.add(m.Finding(company_run_id=cr.id, field="revenue", value=None,
                        kind="unknown", reason="not published"))
        person = m.Person(company_run_id=cr.id, name="Philip Soon", role="Director")
        s.add(person)
        s.flush()
        s.add(m.Evidence(person_id=person.id, source_url="https://find-and-update.company-information.service.gov.uk/company/1/officers", quote="Philip Soon"))
        s.add(m.Qualification(company_run_id=cr.id, score=55.0, tier="moderate",
                              rationale="partly", icp_match={"matched": ["sector"],
                                                             "missed": ["signal"]}))
        s.add(m.Step(company_run_id=cr.id, seq=1, type="tool_result",
                     tool_name="open_page", tool_input={"url": "https://asiagate.co.uk"},
                     output_summary="Read directly", url="https://asiagate.co.uk",
                     duration_ms=92, branch_id=branch.id))
        s.add(m.Step(company_run_id=cr.id, seq=2, type="tool_result",
                     tool_name="open_page",
                     tool_input={"url": "https://x.example", "escalated": True,
                                 "reason": "only 479 characters of text"},
                     output_summary="Opened in the browser", duration_ms=2100))
        ids = (run.id, cr.id)

    with TestClient(create_app(settings)) as c:
        c.ids = ids
        yield c
    reset_engine()


# ------------------------------------------------------------------- reading


def test_health_reports_what_it_is_pointed_at(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["model"] and body["engine"] and body["depth"]
    assert body["api_key"] is False
    assert body["running"] is False


def test_totals_counts_the_database(client):
    body = client.get("/api/totals").json()
    assert body["runs"] == 1
    assert body["findings"] == 4
    assert body["people"] == 1


def test_the_run_list_summarises_progress_and_spend(client):
    run_id, _ = client.ids
    rows = client.get("/api/runs").json()
    assert [r["id"] for r in rows] == [run_id]
    row = rows[0]
    assert row["label"] == "november"
    assert row["companies"] == 1 and row["companies_done"] == 1
    assert row["cost_usd"] == pytest.approx(0.0241)


def test_a_run_carries_its_companies(client):
    run_id, cr_id = client.ids
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["objectives"] == "who runs it"
    company = body["companies"][0]
    assert company["id"] == cr_id
    assert company["domain"] == "asiagate.co.uk"
    assert company["facts"] == 3 and company["unknowns"] == 1
    assert company["tier"] == "moderate"


def test_a_company_comes_back_as_fixed_columns(client):
    """The page renders the profile, so the schema has to arrive with it."""
    _, cr_id = client.ids
    body = client.get(f"/api/company-runs/{cr_id}").json()
    fields = {cell["field"]: cell for cell in body["profile"]}
    assert fields["legal_name"]["value"] == "ASIAGATE LTD"
    assert fields["registration_number"]["value"] == "14834017"
    # An invented field name still lands in a column.
    assert fields["registration_number"]["filled"]
    # And an empty column is present rather than absent.
    assert fields["employee_count"]["filled"] is False
    assert "employee_count" in body["missing"]


def test_a_company_carries_its_evidence_people_and_branches(client):
    _, cr_id = client.ids
    body = client.get(f"/api/company-runs/{cr_id}").json()
    assert body["people"][0]["name"] == "Philip Soon"
    assert body["branches"][0]["name"] == "identity"
    finding = next(f for f in body["findings"] if f["field"] == "legal_name")
    assert finding["evidence"][0]["tier_label"] == "registry"
    assert body["qualification"]["score"] == 55.0


def test_the_trace_marks_which_pages_needed_a_browser(client):
    """The escalation is the interesting part of a trace, so the page gets it."""
    _, cr_id = client.ids
    steps = client.get(f"/api/company-runs/{cr_id}/steps").json()
    assert [s["seq"] for s in steps] == [1, 2]
    assert steps[0]["escalated"] is False
    assert steps[1]["escalated"] is True
    assert "479 characters" in steps[1]["reason"]


def test_missing_things_are_404_not_500(client):
    assert client.get("/api/runs/9999").status_code == 404
    assert client.get("/api/company-runs/9999").status_code == 404


# ------------------------------------------------------------------ spending


def test_a_run_without_a_goal_is_refused(client):
    response = client.post("/api/runs", json={"domains": ["example.com"]})
    assert response.status_code == 400
    assert "no goal" in response.json()["detail"]


def test_a_run_without_a_key_is_refused(client):
    response = client.post(
        "/api/runs",
        json={"domains": ["example.com"], "objectives": "who runs it"},
    )
    assert response.status_code == 400
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_an_empty_domain_list_is_refused(client):
    response = client.post(
        "/api/runs", json={"domains": [" "], "objectives": "who runs it"}
    )
    assert response.status_code == 400


def test_an_unknown_depth_is_refused(client, monkeypatch):
    response = client.post(
        "/api/runs",
        json={"domains": ["example.com"], "objectives": "x", "depth": "very deep"},
    )
    assert response.status_code == 400
    assert "depth" in response.json()["detail"]
