"""Quote verification: the check on Scout's central claim.

Two halves. The matcher is pure and tested on text alone, including the cases that
decide whether the metric can be trusted -- a real paraphrase must score above the
threshold and unrelated prose of similar length must score below it. Then the whole
command runs against the fixture site through the database, with a quote planted to
be wrong.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from scout.browser.session import BrowserSession
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope
from scout.verify import (
    PARAPHRASE_RATIO,
    Verdict,
    best_match_ratio,
    classify,
    normalise,
    strip_markdown,
    strip_snapshot_artifacts,
    verify_run,
)

PAGE = (
    "About Northwind Sensors. "
    "Industry: Industrial IoT hardware manufacturing. "
    "Headquarters: Coventry, United Kingdom. "
    "We employ just over 140 people across three sites, and our active markets "
    "are the United Kingdom, Germany and Poland."
)


# ------------------------------------------------------------------- normalising


def test_normalise_folds_typography_and_whitespace():
    assert normalise("  Hello   World  ") == "hello world"
    # A page renders curly quotes and em dashes; a model reproduces straight ones.
    assert normalise("Monzo’s “Flex” — now") == normalise(
        "Monzo's \"Flex\" - now"
    )
    assert normalise("non breaking") == "non breaking"


# ----------------------------------------------------------------- the matcher


def test_exact_quote_is_verbatim():
    verdict, ratio = classify("Headquarters: Coventry, United Kingdom", PAGE)
    assert verdict is Verdict.VERBATIM
    assert ratio == 1.0


def test_case_and_spacing_differences_are_still_verbatim():
    verdict, _ = classify("headquarters:   COVENTRY,  united kingdom", PAGE)
    assert verdict is Verdict.VERBATIM


def test_a_reworded_quote_is_a_paraphrase_not_an_invention():
    """The model dropped two words and changed one. Still clearly this sentence."""
    verdict, ratio = classify("We employ just over 140 people across sites", PAGE)
    assert verdict is Verdict.PARAPHRASE
    assert ratio >= PARAPHRASE_RATIO


def test_a_fabricated_quote_is_not_found():
    verdict, ratio = classify(
        "Our headquarters are in Manchester and we employ 4,000 staff", PAGE
    )
    assert verdict is Verdict.NOT_FOUND
    assert ratio < PARAPHRASE_RATIO


def test_a_plausible_but_wrong_number_is_not_found():
    """The dangerous case: right sentence shape, wrong fact.

    If this scored as a paraphrase the metric would wave through exactly the error
    it exists to catch.
    """
    verdict, ratio = classify("Headquarters: Coventry, Australia", PAGE)
    assert verdict is not Verdict.VERBATIM
    assert ratio < 1.0


def test_unrelated_prose_of_similar_length_scores_below_the_threshold():
    """Guards the threshold from the other side.

    English prose shares enough letters that any two sentences score some
    similarity. If that noise reached PARAPHRASE_RATIO the metric would be useless.
    """
    unrelated = "The quarterly report was published alongside the annual accounts"
    assert best_match_ratio(normalise(unrelated), normalise(PAGE)) < PARAPHRASE_RATIO


def test_snapshot_formatting_is_stripped_before_matching():
    """The agent quotes what it was shown, and a snapshot line carries our markup.

    A real case: the agent quoted a careers listing as
    ``Lead ML Scientist, Cardiff -> https://job-boards.greenhouse.io/...``. The arrow
    and href are this program's rendering, so matching them against page text marked
    an honest quote as fabricated.
    """
    page = "Lead Machine Learning Scientist, FinCrime Cardiff, London or Remote (UK)"
    quoted_as_shown = (
        "[ref=12] <a> Lead Machine Learning Scientist, FinCrime Cardiff, London or "
        "Remote (UK) -> https://job-boards.greenhouse.io/monzo/jobs/123"
    )
    assert strip_snapshot_artifacts(quoted_as_shown).startswith("Lead Machine")
    assert classify(quoted_as_shown, page)[0] is Verdict.VERBATIM


def test_an_elided_quote_passes_only_if_every_part_is_present():
    page = (
        "LAYFIELD, Diana Louise Patricia. Appointed 02 February 2026. "
        "Role ACTIVE Director. Nationality British."
    )
    assert classify("LAYFIELD, Diana Louise Patricia ... Role ACTIVE Director", page)[
        0
    ] is Verdict.VERBATIM
    # One half real, the other invented, is not a pass.
    verdict, _ = classify(
        "LAYFIELD, Diana Louise Patricia ... Role RESIGNED Company Secretary", page
    )
    assert verdict is not Verdict.VERBATIM


def test_markdown_from_web_fetch_is_reduced_to_its_words():
    """web_fetch returns pages as Markdown, the browser returns rendered text.

    A real case: the agent quoted "### Products - [Current accounts](https://...)",
    which no rendered page contains, and was marked a fabrication for it.
    """
    page = "Products Current accounts Business accounts Savings"
    quoted = (
        "### Products\n- [Current accounts](https://monzo.com/current-account)\n"
        "- [Business accounts](https://monzo.com/business-banking)\n"
        "- [Savings](https://monzo.com/savings-isas)"
    )
    assert "](" not in strip_markdown(quoted)
    assert classify(quoted, page)[0] in (Verdict.VERBATIM, Verdict.PARAPHRASE)


def test_stripping_markdown_cannot_rescue_an_invented_quote():
    """The syntax goes; no word is added, so a fabrication still fails."""
    page = "Products Current accounts Business accounts"
    invented = "### Products\n- [Mortgages](https://x)\n- [Car insurance](https://y)"
    assert classify(invented, page)[0] is Verdict.NOT_FOUND


def test_a_quote_that_is_only_snapshot_markup_is_no_quote():
    assert classify("[ref=7] <button>", "any page")[0] is Verdict.NO_QUOTE


def test_missing_quote_and_unreachable_page_are_distinct():
    assert classify(None, PAGE)[0] is Verdict.NO_QUOTE
    assert classify("   ", PAGE)[0] is Verdict.NO_QUOTE
    # A page we could not load is not evidence against the quote.
    assert classify("anything", None)[0] is Verdict.UNREACHABLE


def test_quote_longer_than_the_page_does_not_crash():
    verdict, _ = classify("word " * 200, "short page")
    assert verdict is Verdict.NOT_FOUND


# ------------------------------------------------------- end to end, via the DB


@pytest.fixture
def run_with_evidence(tmp_path, fixture_site):
    """A finished run holding one true quote, one paraphrase and one fabrication."""
    settings = replace(load_settings(), data_dir=tmp_path, headed=False)
    reset_engine()
    init_db(settings)

    about = f"{fixture_site}/about.html"
    with session_scope(settings) as s:
        company = m.Company(domain="northwind.example")
        run = m.Run(model="test", effort="medium", engine="browser")
        s.add_all([company, run])
        s.flush()
        cr = m.CompanyRun(run_id=run.id, company_id=company.id)
        s.add(cr)
        s.flush()

        planted = [
            # Rendered by JavaScript from data.json, so this is a real check that
            # verification sees the page as the browser renders it.
            ("headquarters_location", "fact", "Headquarters: Coventry, United Kingdom"),
            ("industry", "fact", "Industry: Industrial IoT hardware"),
            ("company_size", "fact", "We have 9,000 employees worldwide"),
        ]
        for field, kind, quote in planted:
            f = m.Finding(
                company_run_id=cr.id, field=field, value="v", kind=kind
            )
            s.add(f)
            s.flush()
            s.add(m.Evidence(finding_id=f.id, source_url=about, quote=quote))

        # An explicit unknown carries no quote and must not be counted.
        s.add(
            m.Finding(
                company_run_id=cr.id,
                field="revenue",
                value="unknown",
                kind=m.FindingKind.UNKNOWN.value,
            )
        )
        run_id = run.id

    return settings, run_id


def test_verify_run_separates_real_quotes_from_invented_ones(
    run_with_evidence, tmp_path
):
    """The two real quotes are JS-rendered, so this also proves verification reads
    the rendered page: against served HTML both would look fabricated."""
    settings, run_id = run_with_evidence
    with BrowserSession(
        screenshot_dir=tmp_path / "shots", headed=False, timeout_ms=10_000
    ) as browser:
        report = verify_run(settings, run_id, session=browser)

    by_field = {c.field: c for c in report.checks}
    assert by_field["headquarters_location"].verdict is Verdict.VERBATIM
    assert by_field["industry"].verdict is Verdict.VERBATIM
    assert by_field["company_size"].verdict is Verdict.NOT_FOUND

    # The unknown has no evidence row, so it is not in the report at all.
    assert "revenue" not in by_field

    assert report.accuracy == pytest.approx(2 / 3)
    # One URL, loaded once, for three claims.
    assert report.pages_loaded == 1


def test_accuracy_is_none_when_nothing_could_be_checked(tmp_path):
    settings = replace(load_settings(), data_dir=tmp_path, headed=False)
    reset_engine()
    init_db(settings)
    with session_scope(settings) as s:
        company = m.Company(domain="empty.example")
        run = m.Run(model="test", effort="medium", engine="browser")
        s.add_all([company, run])
        s.flush()
        s.add(m.CompanyRun(run_id=run.id, company_id=company.id))
        run_id = run.id

    report = verify_run(settings, run_id)
    assert report.checks == []
    assert report.accuracy is None


# ------------------------------------------------- one page, two honest renderings


def test_the_kindest_honest_rendering_decides():
    """The agent reads most pages over HTTP; the browser renders the rest. Markup
    that becomes visible text in one is dropped by the other, so a correct quote can
    fail against the wrong one. Measured at 77% on a run whose facts were right."""
    from scout.verify import _best

    http_text = "Contact Us . Give us a call on 03450 678080 or email info@acme.co.uk"
    rendered = "Give us a call on 03450 678080"
    verdict, _ = _best("Contact Us . Give us a call on 03450 678080", [rendered, http_text])
    assert verdict is Verdict.VERBATIM


def test_taking_the_best_cannot_rescue_an_invention():
    """Both texts are the real page, so a sentence in neither is still absent.
    Without this the metric would wave through exactly what it exists to catch."""
    from scout.verify import _best

    verdict, ratio = _best(
        "We employ 9,000 people and are headquartered in Zurich",
        ["Acme Ltd is based in Salford.", "Acme Ltd . Based in Salford, England."],
    )
    assert verdict is Verdict.NOT_FOUND
    assert ratio < PARAPHRASE_RATIO


def test_a_page_reachable_by_neither_route_is_unreachable_not_wrong():
    from scout.verify import _best

    assert _best("anything", [None, None])[0] is Verdict.UNREACHABLE
    assert _best("anything", [])[0] is Verdict.UNREACHABLE


def test_a_missing_quote_stays_a_missing_quote():
    from scout.verify import _best

    assert _best(None, ["a page"])[0] is Verdict.NO_QUOTE


def test_fetching_settles_most_pages_without_a_browser(run_with_evidence):
    """The browser is for the pages plain HTTP could not account for. Loading one for
    every page would cost more than the research run did."""
    settings, run_id = run_with_evidence
    report = verify_run(settings, run_id)
    assert report.pages_fetched >= 1
    # The fixture page is JavaScript-rendered, so this one does need the browser.
    assert report.pages_loaded >= 1


def test_the_planted_fabrication_still_fails_through_both_routes(run_with_evidence):
    settings, run_id = run_with_evidence
    report = verify_run(settings, run_id)
    by_field = {c.field: c for c in report.checks}
    assert by_field["company_size"].verdict is Verdict.NOT_FOUND
    assert by_field["headquarters_location"].verdict is Verdict.VERBATIM


# ------------------------------------------- scripts that do not separate words


JP_PAGE = (
    "株式会社Stay Factory 会社概要 代表者 吉田 弘志 本社所在地 〒556-0005 "
    "大阪市浪速区日本橋4丁目2番15号 TEL 06-6532-2233（代表） 従業員数 300名 "
    "業種 国内外における宿泊業、飲食業。被保険者数 118人 法人種別 株式会社"
)


def test_a_japanese_quote_is_matched_by_character_not_by_word():
    """Measured on a real run: four correct Japanese quotes read as fabricated,
    because a Japanese sentence splits into one or two "words", so a window measured
    in words is a window of the whole quote and the scan never aligns."""
    from scout.verify import written_without_spaces

    quote = "代表者 吉田 弘志"
    assert written_without_spaces(quote)
    verdict, ratio = classify(quote, JP_PAGE)
    assert verdict is Verdict.VERBATIM, ratio


@pytest.mark.parametrize(
    "quote",
    ["被保険者数 118人", "法人種別 株式会社", "本社所在地 〒556-0005 大阪市浪速区日本橋4丁目2番15号"],
)
def test_real_japanese_quotes_pass(quote):
    assert classify(quote, JP_PAGE)[0] in (Verdict.VERBATIM, Verdict.PARAPHRASE)


def test_an_invented_japanese_quote_still_fails():
    """The window changes; the standard does not."""
    verdict, ratio = classify("代表者 田中 太郎 資本金 5億円 従業員数 9000名", JP_PAGE)
    assert verdict is Verdict.NOT_FOUND
    assert ratio < PARAPHRASE_RATIO


def test_a_wrong_number_in_japanese_is_not_waved_through():
    """The dangerous case, in a script where everything looks similar."""
    verdict, _ = classify("従業員数 900名", JP_PAGE)
    assert verdict is not Verdict.VERBATIM


def test_english_is_unaffected_by_the_japanese_path():
    from scout.verify import written_without_spaces

    assert not written_without_spaces(PAGE)
    assert not written_without_spaces("Headquarters: Coventry, United Kingdom")
    assert classify("Headquarters: Coventry, United Kingdom", PAGE)[0] is Verdict.VERBATIM


def test_a_latin_name_inside_japanese_text_is_still_handled():
    """Company names stay in Latin script inside an otherwise Japanese page."""
    assert classify("株式会社Stay Factory", JP_PAGE)[0] is Verdict.VERBATIM


def test_an_elided_japanese_quote_is_split_on_length():
    """A Japanese fragment has too few "words" to pass a word-count filter."""
    quote = "代表者 吉田 弘志 ... 本社所在地 〒556-0005 大阪市浪速区日本橋4丁目2番15号"
    assert classify(quote, JP_PAGE)[0] in (Verdict.VERBATIM, Verdict.PARAPHRASE)
    bad = "代表者 吉田 弘志 ... 本社所在地 東京都千代田区丸の内1丁目1番1号"
    assert classify(bad, JP_PAGE)[0] is not Verdict.VERBATIM
