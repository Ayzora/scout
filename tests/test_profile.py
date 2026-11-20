"""One row per company, with the same columns every time.

Three runs on one company recorded the same facts under `identifier_ods_code`,
`total_headcount_2025_26_narrative`, `operational_pressure_handover_delays_march_2026`
and `registration_number (Companies House)`. Each is a fair name for what it holds and
no two runs agree on them, so nothing could be sorted, filtered or written into a CRM.

The field names in these tests are taken verbatim from those runs. That is the point:
the alias map exists because a prompt is a request, and the output shape has to hold
whether or not the request was followed.
"""

from __future__ import annotations

import pytest

from scout.agent.sources import TIER_COMPANY, TIER_REGISTRY
from scout.profile import (
    BY_NAME,
    CORE_COLUMNS,
    build_profile,
    canonical_field,
    describe_columns,
)


def _row(field, value, url="https://example.com/a", kind="fact", fid=None):
    return {
        "field": field, "value": value, "kind": kind,
        "source_url": url, "finding_id": fid,
    }


# ------------------------------------------------------------------- the mapping


@pytest.mark.parametrize(
    "recorded,column",
    [
        # Taken from real runs.
        ("identifier_ods_code", "registration_number"),
        ("registration_number (Companies House)", "registration_number"),
        ("total_headcount_2025_26_narrative", "employee_count"),
        ("headcount_2024_25_quality_account", "employee_count"),
        ("total_income_from_activities_2025_26", "revenue"),
        ("operational_pressure_handover_delays_march_2026", "recent_developments"),
        ("buying_signal:operational_pressure", "recent_developments"),
        ("geographic_scope", "geographic_presence"),
        ("company_name", "trading_name"),
        ("formation", "founded"),
        ("commissioners", "ownership"),
        # Ordinary spellings.
        ("Company Size", "employee_count"),
        ("hq", "headquarters_location"),
        ("sector", "industry"),
    ],
)
def test_recorded_names_map_onto_columns(recorded, column):
    assert canonical_field(recorded) == column


def test_a_name_that_fits_nothing_is_not_forced_into_a_column():
    """An extra is kept and reported. A wrong column would be silent and worse."""
    assert canonical_field("board_page_date") is None
    assert canonical_field("favourite_colour") is None


def test_every_column_maps_to_itself():
    for name in BY_NAME:
        assert canonical_field(name) == name


def test_the_vocabulary_has_no_ambiguous_aliases():
    """Two columns claiming one alias would make the mapping depend on definition
    order, which is exactly the instability this replaces."""
    seen: dict[str, str] = {}
    for column in BY_NAME.values():
        for alias in (column.name, *column.aliases):
            assert alias not in seen, f"{alias!r} claimed by {seen.get(alias)} and {column.name}"
            seen[alias] = column.name


def test_the_prompt_lists_the_columns():
    described = describe_columns()
    for name in BY_NAME:
        assert f"`{name}`" in described


# -------------------------------------------------------------- choosing a value


def test_the_best_supported_value_wins_not_the_first_recorded():
    """Order of arrival is an accident of which branch ran first."""
    rows = [
        _row("employee_count", "about 5,000", "https://en.wikipedia.org/wiki/X"),
        _row("employee_count", "7,350 as at 31 March 2026", "https://acme.com/report"),
    ]
    profile = build_profile(rows, "acme.com")
    cell = profile.cells["employee_count"]
    assert cell.value.startswith("7,350")
    assert cell.tier == TIER_COMPANY
    # The value not chosen is kept, not deleted.
    assert "about 5,000" in cell.alternates


def test_a_corroborated_value_outranks_a_better_tier_alone():
    rows = [
        _row("industry", "Retail banking", "https://acme.com/about"),
        _row("industry", "Fintech", "https://press.example/a"),
        _row("industry", "Fintech", "https://other.example/b"),
        _row("industry", "Fintech", "https://register.gov.uk/x"),
    ]
    cell = build_profile(rows, "acme.com").cells["industry"]
    assert cell.value == "Fintech"
    assert cell.corroborated


def test_a_fact_outranks_an_inference_from_the_same_sort_of_source():
    rows = [
        _row("industry", "Guessed from the homepage", "https://acme.com/a", kind="inference"),
        _row("industry", "Retail banking", "https://acme.com/b"),
    ]
    assert build_profile(rows, "acme.com").cells["industry"].value == "Retail banking"


def test_a_multi_valued_column_keeps_every_distinct_entry():
    rows = [
        _row("products_services", "Current accounts", "https://acme.com/a"),
        _row("products", "Business banking", "https://acme.com/b"),
        _row("offerings", "current accounts", "https://acme.com/c"),
    ]
    cell = build_profile(rows, "acme.com").cells["products_services"]
    assert len(cell.values) == 2, cell.values
    assert "Current accounts" in cell.values and "Business banking" in cell.values


def test_unknowns_do_not_fill_a_column():
    rows = [
        {"field": "revenue", "value": None, "kind": "unknown", "source_url": ""},
    ]
    profile = build_profile(rows, "acme.com")
    assert not profile.cells["revenue"].filled
    assert "revenue" in profile.missing()


def test_nothing_recorded_is_lost():
    rows = [_row("board_page_date", "last reviewed 10/04/2026", "https://acme.com/b")]
    profile = build_profile(rows, "acme.com")
    assert [e.field for e in profile.extras] == ["board_page_date"]
    assert profile.extras[0].sources == ["acme.com"]


def test_the_column_a_value_came_from_is_traceable():
    rows = [_row("identifier_ods_code", "RYA", "https://directory.spineservices.nhs.uk/x")]
    cell = build_profile(rows, "wmas.nhs.uk").cells["registration_number"]
    assert cell.recorded_as == ["identifier_ods_code"]


# ------------------------------------------------------------------- derivation


def test_a_name_from_a_register_fills_the_legal_name():
    """Without this, a run that reads the register and files the answer under
    `company_name` leaves the legal-name column empty while holding the answer."""
    rows = [_row("company_name", "ACME HOLDINGS PLC",
                 "https://find-and-update.company-information.service.gov.uk/company/1")]
    profile = build_profile(rows, "acme.com")
    legal = profile.cells["legal_name"]
    assert legal.value == "ACME HOLDINGS PLC"
    assert legal.tier == TIER_REGISTRY
    assert "derived from the register entry" in legal.recorded_as


def test_a_name_from_the_companys_own_site_is_not_assumed_to_be_the_legal_name():
    rows = [_row("company_name", "Acme", "https://acme.com/about")]
    profile = build_profile(rows, "acme.com")
    assert profile.cells["trading_name"].value == "Acme"
    assert not profile.cells["legal_name"].filled


def test_an_explicit_legal_name_is_not_overwritten():
    rows = [
        _row("legal_name", "ACME HOLDINGS PLC", "https://acme.com/imprint"),
        _row("company_name", "Acme", "https://register.gov.uk/x"),
    ]
    assert build_profile(rows, "acme.com").cells["legal_name"].value == "ACME HOLDINGS PLC"


# ----------------------------------------------------------------- the flat row


def test_the_row_has_the_same_keys_whatever_was_found():
    rich = build_profile([_row("company_name", "Acme")], "acme.com").row()
    empty = build_profile([], "acme.com").row()
    assert rich.keys() == empty.keys() == BY_NAME.keys()
    assert all(isinstance(v, str) for v in empty.values())


def test_coverage_counts_the_core_columns():
    rows = [_row(name, "x", f"https://acme.com/{name}") for name in CORE_COLUMNS[:3]]
    profile = build_profile(rows, "acme.com")
    assert profile.coverage() == pytest.approx(3 / len(CORE_COLUMNS))
    assert set(profile.missing()) == set(CORE_COLUMNS[3:])


def test_a_json_quoted_value_is_cleaned():
    rows = [_row("industry", '"Retail  banking"')]
    assert build_profile(rows, "acme.com").cells["industry"].value == "Retail banking"
