"""Corroboration, source weighting and conflict detection.

All pure: no browser, no model, no database. These two modules decide what a deep run
is allowed to claim is well-established, so the edge cases are the point.
"""

from __future__ import annotations

from scout.agent.merge import (
    SAME_VALUE_OVERLAP,
    Claim,
    is_single_valued,
    merge_findings,
    normalise_value,
    values_agree,
)
from scout.agent.sources import (
    TIER_AGGREGATOR,
    TIER_COMPANY,
    TIER_PRESS,
    TIER_REGISTRY,
    host_of,
    is_same_company,
    source_tier,
)

COMPANIES_HOUSE = (
    "https://find-and-update.company-information.service.gov.uk/company/09446231"
)


# ----------------------------------------------------------------- source tiers


def test_hosts_are_normalised():
    assert host_of("https://www.Monzo.com/about") == "monzo.com"
    assert host_of("https://monzo.com:443/x") == "monzo.com"
    assert host_of("not a url") == ""


def test_tiers_from_real_urls_seen_in_run_3():
    own = "monzo.com"
    assert source_tier(COMPANIES_HOUSE, own) == TIER_REGISTRY
    assert source_tier("https://monzo.com/about", own) == TIER_COMPANY
    assert source_tier("https://en.wikipedia.org/wiki/Monzo", own) == TIER_AGGREGATOR
    assert source_tier("https://app.dealroom.co/companies/monzo", own) == TIER_AGGREGATOR
    assert source_tier("https://www.finextra.com/newsarticle/1", own) == TIER_PRESS


def test_subdomains_of_the_company_count_as_the_company():
    assert is_same_company("community.monzo.com", "monzo.com")
    assert source_tier("https://community.monzo.com/t/1", "monzo.com") == TIER_COMPANY


def test_a_registry_outranks_the_company_itself():
    """A filing is a stronger claim than a marketing page about the same company."""
    assert TIER_REGISTRY < TIER_COMPANY
    # Even when the registry page is reached while researching that company.
    assert source_tier(COMPANIES_HOUSE, "monzo.com") == TIER_REGISTRY


def test_unknown_hosts_default_to_press_not_to_nothing():
    assert source_tier("https://some-trade-journal.example/x", "monzo.com") == TIER_PRESS
    assert source_tier("https://anything.example/x", None) == TIER_PRESS


def test_a_different_tld_for_the_same_brand_is_a_known_gap():
    """Documented limitation rather than an accident: string comparison cannot tell
    that monzo.co.uk is the same company as monzo.com."""
    assert not is_same_company("monzo.co.uk", "monzo.com")


# ------------------------------------------------------------- value comparison


def test_the_same_claim_worded_differently_agrees():
    assert values_agree("Coventry, United Kingdom", "coventry united kingdom")
    assert values_agree("United Kingdom, Germany and Poland", "Germany, Poland, United Kingdom")


def test_different_places_do_not_agree():
    assert not values_agree("Coventry, United Kingdom", "Manchester, United Kingdom")


def test_numbers_must_match_exactly():
    """The dangerous near-miss: similar wording, different figure.

    Word overlap alone would merge these, and a wrong headcount would then inherit
    the corroboration of the right one.
    """
    assert not values_agree("just over 140 people", "just over 1400 people")
    assert not values_agree("revenue of 1.7bn", "revenue of 1.2bn")
    assert values_agree("just over 140 people", "140 people, just over")


def test_normalise_is_order_and_punctuation_insensitive():
    assert normalise_value("Coventry, United Kingdom") == normalise_value(
        "united kingdom coventry"
    )


def test_overlap_threshold_is_a_named_constant():
    assert 0.5 < SAME_VALUE_OVERLAP <= 1.0


# ---------------------------------------------------------------- corroboration


def _row(field, value, url, tier, kind="fact", fid=None, branch=None):
    return {
        "field": field,
        "value": value,
        "kind": kind,
        "source_url": url,
        "source_tier": tier,
        "finding_id": fid,
        "branch": branch,
    }


def test_two_independent_sources_with_a_primary_is_corroborated():
    result = merge_findings(
        [
            _row("headquarters_location", "Coventry, UK", "https://monzo.com/about", TIER_COMPANY),
            _row("headquarters_location", "Coventry UK", COMPANIES_HOUSE, TIER_REGISTRY),
        ]
    )
    assert len(result.claims) == 1
    claim = result.claims[0]
    assert claim.source_count == 2
    assert claim.corroborated
    assert claim.best_tier == TIER_REGISTRY


def test_several_pages_of_one_site_are_one_source():
    """Otherwise a claim could corroborate itself by being repeated."""
    result = merge_findings(
        [
            _row("industry", "Banking", "https://monzo.com/about", TIER_COMPANY),
            _row("industry", "Banking", "https://monzo.com/press", TIER_COMPANY),
            _row("industry", "Banking", "https://monzo.com/ie", TIER_COMPANY),
        ]
    )
    claim = result.claims[0]
    assert claim.source_count == 1
    assert not claim.corroborated


def test_two_aggregators_do_not_corroborate_each_other():
    """They commonly copy the same original, so agreement proves little."""
    result = merge_findings(
        [
            _row("company_size", "5275 employees", "https://en.wikipedia.org/wiki/Monzo", TIER_AGGREGATOR),
            _row("company_size", "5275 employees", "https://app.dealroom.co/x", TIER_AGGREGATOR),
        ]
    )
    claim = result.claims[0]
    assert claim.source_count == 2
    assert not claim.corroborated


def test_a_single_source_is_flagged_never_dropped():
    result = merge_findings(
        [_row("revenue", "1.7bn", "https://monzo.com/annual-report", TIER_COMPANY)]
    )
    assert len(result.claims) == 1
    assert result.single_sourced == result.claims
    assert result.stats()["corroboration_rate"] == 0.0


# -------------------------------------------------------------------- conflicts


def test_two_incompatible_facts_for_one_field_are_a_conflict():
    result = merge_findings(
        [
            _row("headquarters_location", "Coventry, UK", "https://monzo.com/about", TIER_COMPANY),
            _row("headquarters_location", "London, UK", "https://news.example/x", TIER_PRESS),
        ]
    )
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.field == "headquarters_location"
    assert "vs" in conflict.describe()


def test_a_more_precise_value_is_not_a_conflict():
    """From the first run against real findings, which reported pages of these.

    "London HQ" and "London, UK (near Liverpool Street station)" are the same fact at
    different precision. Calling that a contradiction hid the real disagreements.
    """
    result = merge_findings(
        [
            _row("headquarters_location", "London HQ", "https://monzo.com/", TIER_COMPANY),
            _row(
                "headquarters_location",
                "London, UK (near Liverpool Street station); additional office in Cardiff",
                "https://monzo.com/about",
                TIER_COMPANY,
            ),
        ]
    )
    assert result.conflicts == []


def test_a_precise_value_that_contradicts_a_number_is_still_a_conflict():
    """Refinement must not become a loophole for a wrong figure."""
    result = merge_findings(
        [
            _row("founded", "founded 2015", "https://monzo.com/", TIER_COMPANY),
            _row("founded", "founded 2019 in London", "https://x.example/", TIER_PRESS),
        ]
    )
    assert len(result.conflicts) == 1


def test_catch_all_fields_are_not_treated_as_single_valued():
    """company_size is used for customers, revenue, headcount and assets alike."""
    assert not is_single_valued("company_size")
    assert not is_single_valued("company_name")
    assert not is_single_valued("industry")
    result = merge_findings(
        [
            _row("company_size", "16 million customers", "https://monzo.com/", TIER_COMPANY),
            _row("company_size", "5,275 employees", "https://en.wikipedia.org/x", TIER_AGGREGATOR),
            _row("company_size", "total assets 27.6bn", "https://monzo.com/ar", TIER_COMPANY),
        ]
    )
    assert result.conflicts == []


def test_a_list_field_holding_several_values_is_not_a_conflict():
    """A company genuinely has many products; flagging that would bury real conflicts."""
    result = merge_findings(
        [
            _row("products_services", "Current accounts", "https://monzo.com/", TIER_COMPANY),
            _row("products_services", "Business banking", "https://monzo.com/business", TIER_COMPANY),
            _row("products_services", "Loans", "https://monzo.com/loans", TIER_COMPANY),
        ]
    )
    assert result.conflicts == []
    assert len(result.claims) == 3
    assert not is_single_valued("products_services")
    assert is_single_valued("headquarters_location")


def test_an_inference_does_not_conflict_with_a_fact():
    """Conflicts are for claims that cannot both be observed. An inference is already
    marked as reasoning rather than observation."""
    result = merge_findings(
        [
            _row("company_size", "140 people", "https://monzo.com/about", TIER_COMPANY),
            _row("company_size", "about 200 people", "https://x.example/", TIER_PRESS, kind="inference"),
        ]
    )
    assert result.conflicts == []


def test_stats_summarise_a_whole_company():
    result = merge_findings(
        [
            _row("industry", "Banking", "https://monzo.com/", TIER_COMPANY),
            _row("industry", "Banking", COMPANIES_HOUSE, TIER_REGISTRY),
            _row("revenue", "1.7bn", "https://monzo.com/ar", TIER_COMPANY),
        ]
    )
    stats = result.stats()
    assert stats["claims"] == 2
    assert stats["corroborated"] == 1
    assert stats["single_sourced"] == 1
    assert stats["corroboration_rate"] == 0.5


def test_branches_that_found_the_same_claim_are_recorded():
    """Useful for spotting branches duplicating each other's work."""
    result = merge_findings(
        [
            _row("industry", "Banking", "https://monzo.com/", TIER_COMPANY, branch="products"),
            _row("industry", "Banking", COMPANIES_HOUSE, TIER_REGISTRY, branch="legal"),
        ]
    )
    assert result.claims[0].branches == {"products", "legal"}


def test_empty_input_reports_no_data_rather_than_a_perfect_score():
    result = merge_findings([])
    assert result.claims == []
    assert result.stats()["corroboration_rate"] is None
