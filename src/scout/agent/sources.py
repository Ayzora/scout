"""How much weight a source carries, decided from its host.

Corroboration is only meaningful if "two sources" means two *independent* sources of
reasonable standing. Two blog posts repeating one press release is not corroboration,
and a statutory filing is not the same kind of claim as a Wikipedia edit.

Tiers are derived in Python from the URL rather than asked of the model, so they are a
property of where a claim came from instead of the model's opinion of it. The lists are
deliberately short and will need growing against real runs; everything unlisted falls to
tier 3, which means ``corroborated`` is easier to reach than it ideally would be.
"""

from __future__ import annotations

from urllib.parse import urlparse

TIER_REGISTRY = 1  # statutory registers and regulatory filings
TIER_COMPANY = 2  # the company speaking about itself
TIER_PRESS = 3  # everyone else: the default
TIER_AGGREGATOR = 4  # compiled or user-editable databases

TIER_NAMES = {
    TIER_REGISTRY: "registry",
    TIER_COMPANY: "company",
    TIER_PRESS: "press",
    TIER_AGGREGATOR: "aggregator",
}

# Primary records. A company's filing with its own regulator is the strongest claim
# available about its legal identity, officers and accounts.
REGISTRY_HOSTS = frozenset(
    {
        "find-and-update.company-information.service.gov.uk",  # UK Companies House
        "api.company-information.service.gov.uk",
        "sec.gov",  # US EDGAR
        "efts.sec.gov",
        "cro.ie",  # Ireland
        "core.cro.ie",
        "bundesanzeiger.de",  # Germany
        "unternehmensregister.de",
        "handelsregister.de",
        "kvk.nl",  # Netherlands
        "asic.gov.au",  # Australia
        "sedar.com",  # Canada
        "directory.spineservices.nhs.uk",  # NHS organisation directory
        "england.nhs.uk",  # NHS England: licenses and publishes the trust record
        "dsptoolkit.nhs.uk",  # NHS official organisation register
        "digital.nhs.uk",
        "nhsbsa.nhs.uk",
        "odsdatasearchandexport.nhsbsa.nhs.uk",
        "cqc.org.uk",  # UK health and social care regulator
        "fca.org.uk",  # UK financial regulator
        "register.fca.org.uk",
        "gov.uk",
        "europa.eu",
    }
)

# Compiled or openly editable. Often accurate, never primary, and frequently derived
# from one of the sources above -- so two aggregators agreeing is one source, not two.
AGGREGATOR_HOSTS = frozenset(
    {
        "wikipedia.org",
        "wikidata.org",
        "crunchbase.com",
        "dealroom.co",
        "pitchbook.com",
        "cbinsights.com",
        "tracxn.com",
        "owler.com",
        "zoominfo.com",
        "apollo.io",
        "glassdoor.com",
        "glassdoor.co.uk",
        "endole.co.uk",
        "opencorporates.com",
        "companieshouse.rocks",
        "linkedin.com",
        "bloomberg.com/profile",
    }
)


def host_of(url: str) -> str:
    """The lower-cased hostname, without a leading ``www.``."""
    host = (urlparse(url).netloc or "").lower()
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, known: frozenset[str]) -> bool:
    """True when the host is one of ``known`` or a subdomain of one."""
    for entry in known:
        if host == entry or host.endswith("." + entry):
            return True
    return False


def is_same_company(host: str, company_domain: str | None) -> bool:
    """Whether a host belongs to the company itself.

    Subdomains count: ``community.monzo.com`` is still Monzo. A different top-level
    domain for the same brand does not, so ``monzo.co.uk`` would read as press. That
    is a known gap rather than a judgement -- inferring brand identity across TLDs
    needs more than string comparison.
    """
    if not company_domain:
        return False
    own = host_of(f"https://{company_domain}") or company_domain.lower()
    return host == own or host.endswith("." + own)


def source_tier(url: str, company_domain: str | None = None) -> int:
    """The tier for one evidence URL.

    A registry wins over the company's own site, because a filing is a stronger claim
    than a marketing page even when both are about the same company.
    """
    host = host_of(url)
    if not host:
        return TIER_PRESS
    if _matches(host, REGISTRY_HOSTS):
        return TIER_REGISTRY
    if is_same_company(host, company_domain):
        return TIER_COMPANY
    if _matches(host, AGGREGATOR_HOSTS):
        return TIER_AGGREGATOR
    return TIER_PRESS


def tier_name(tier: int | None) -> str:
    return TIER_NAMES.get(tier, "unknown") if tier is not None else "unknown"
