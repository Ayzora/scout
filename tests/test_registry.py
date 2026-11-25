"""Statutory registers, asked directly.

The source hierarchy has always said a register outranks a company's description of
itself, but that ranking only graded evidence after it was collected. Nothing sent the
agent to a register, so it searched the open web for facts a register hands over --
and a search is the one thing billed per call: on one measured run, searches were
most of the bill while every token of browsing and reasoning was a fraction of it.

These tests run against recorded responses, not the live registers, so they are free
and do not fail when a service is down. One test is marked to hit the real NHS
directory, because an offline-only suite would not notice the API changing shape.
"""

from __future__ import annotations

import json

import pytest

from scout.agent.sources import TIER_REGISTRY, source_tier
from scout.registry import (
    RegistryAnswer,
    companies_house_search,
    is_nhs,
    lookup,
    nhs_ods_search,
)

ODS_RESPONSE = {
    "Organisations": [
        {
            "Name": "EXAMPLE AMBULANCE SERVICE NHS TRUST",
            "OrgId": "RKA",
            "Status": "Inactive",
            "PostCode": "DY5 1LX",
            "PrimaryRoleDescription": "NHS TRUST",
            "OrgLink": "https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations/RKA",
        },
        {
            "Name": "EXAMPLE AMBULANCE SERVICE UNIVERSITY NHS FOUNDATION TRUST",
            "OrgId": "ABC",
            "Status": "Active",
            "PostCode": "DY5 1LX",
            "PrimaryRoleDescription": "NHS TRUST",
            "OrgLink": "https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations/RYA",
        },
    ]
}

CH_RESPONSE = {
    "items": [
        {
            "title": "EXAMPLE BANK LIMITED",
            "company_number": "00000001",
            "company_status": "active",
            "company_type": "ltd",
            "date_of_creation": "2015-02-06",
            "address": {"postal_code": "EC2A 4BX"},
        }
    ]
}


@pytest.fixture
def fake_json(monkeypatch):
    """Serve a recorded payload in place of the network."""

    def install(payload, error: Exception | None = None):
        def _get_json(url, headers=None):
            if error is not None:
                raise error
            return payload, None

        monkeypatch.setattr("scout.registry._get_json", _get_json)

    return install


# ---------------------------------------------------------------------- NHS


def test_the_nhs_directory_returns_the_registration_number(fake_json):
    """The field a web search is worst at and a register answers exactly."""
    fake_json(ODS_RESPONSE)
    answer = nhs_ods_search("Example Ambulance Service")
    numbers = {h.number for h in answer.hits}
    assert "ABC" in numbers


def test_a_retired_name_is_returned_with_its_status(fake_json):
    """Aggregators keep a retired name for years. The register marks that entity
    inactive, which is the reason to ask a register at all."""
    fake_json(ODS_RESPONSE)
    by_number = {h.number: h for h in nhs_ods_search("Example Ambulance").hits}
    assert by_number["RKA"].status == "Inactive"
    assert by_number["ABC"].status == "Active"
    assert "UNIVERSITY" in by_number["ABC"].name


def test_every_hit_carries_the_registers_own_url(fake_json):
    fake_json(ODS_RESPONSE)
    for hit in nhs_ods_search("Example Ambulance").hits:
        assert hit.source_url.startswith("https://directory.spineservices.nhs.uk/")


def test_a_register_url_is_tier_one_evidence(fake_json):
    fake_json(ODS_RESPONSE)
    hit = nhs_ods_search("Example Ambulance").hits[0]
    assert source_tier(hit.source_url, "example.nhs.uk") == TIER_REGISTRY


def test_no_match_says_so_rather_than_returning_nothing(fake_json):
    fake_json({"Organisations": []})
    answer = nhs_ods_search("not a real trust")
    assert answer.hits == []
    assert "No NHS organisation" in (answer.note or "")


def test_a_failing_register_degrades_to_a_note(monkeypatch):
    monkeypatch.setattr(
        "scout.registry._get_json", lambda url, headers=None: (None, "HTTP 503")
    )
    answer = nhs_ods_search("anything")
    assert answer.hits == []
    assert "unavailable" in (answer.note or "")


# ------------------------------------------------------------ Companies House


def test_without_a_key_it_hands_back_the_url_rather_than_guessing(monkeypatch):
    """Parsing a results page for a registration number is the kind of inference this
    project exists to avoid. A wrong number is worse than an honest gap."""
    monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
    answer = companies_house_search("Example Bank")
    assert answer.hits == []
    assert answer.url and "find-and-update" in answer.url
    assert "COMPANIES_HOUSE_API_KEY" in (answer.note or "")


def test_with_a_key_it_returns_structured_results(monkeypatch, fake_json):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    fake_json(CH_RESPONSE)
    hit = companies_house_search("Example Bank").hits[0]
    assert hit.number == "00000001"
    assert hit.status == "active"
    assert hit.source_url.endswith("/company/00000001")
    assert hit.as_dict()["incorporated_on"] == "2015-02-06"


def test_an_api_failure_still_offers_the_public_page(monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    monkeypatch.setattr(
        "scout.registry._get_json", lambda url, headers=None: (None, "HTTP 401")
    )
    answer = companies_house_search("Example Bank")
    assert "HTTP 401" in (answer.note or "")
    assert answer.url


# -------------------------------------------------------------------- routing


@pytest.mark.parametrize(
    "domain,name,expected",
    [
        ("example.nhs.uk", None, True),
        (None, "Example Ambulance Service NHS Foundation Trust", True),
        ("example.com", "Example Bank Limited", False),
        ("acme.co.uk", "Acme Holdings", False),
    ],
)
def test_the_right_register_is_chosen(domain, name, expected):
    assert is_nhs(domain, name) is expected


def test_an_nhs_name_with_no_directory_entry_falls_back(monkeypatch):
    """A private provider trading into the health service is a limited company."""
    monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
    monkeypatch.setattr(
        "scout.registry.nhs_ods_search",
        lambda name, limit=5: RegistryAnswer(note="No NHS organisation found."),
    )
    answer = lookup("Example Supply Chain", "something.nhs.uk")
    assert answer.url and "find-and-update" in answer.url


def test_the_answer_serialises_for_the_tool(fake_json):
    fake_json(ODS_RESPONSE)
    payload = json.loads(json.dumps(lookup("Example Ambulance", "example.nhs.uk").as_dict()))
    assert payload["results"][0]["registry"] == "NHS ODS"


# ------------------------------------------------------------------- live check


@pytest.mark.live
def test_the_real_nhs_directory_still_answers_in_this_shape():
    """Free, no key. Guards against the API changing under the offline tests."""
    answer = nhs_ods_search("Ambulance Service")
    assert answer.hits, answer.note
    hit = answer.hits[0]
    assert hit.number and hit.status
    assert hit.source_url.startswith("https://directory.spineservices.nhs.uk/")
