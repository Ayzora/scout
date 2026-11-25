"""Statutory registers, queried directly rather than searched for.

A register is the strongest source for legal identity, and asking one costs nothing
where a web search is billed per request. Companies House needs a key; without one
this returns the public search URL rather than guessing a company number.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from scout.browser.fetcher import USER_AGENT

ODS_BASE = "https://directory.spineservices.nhs.uk/ORD/2-0-0/organisations"
COMPANIES_HOUSE_API = "https://api.company-information.service.gov.uk"
COMPANIES_HOUSE_PUBLIC = "https://find-and-update.company-information.service.gov.uk"

TIMEOUT = 20.0
MAX_RESULTS = 5


@dataclass
class RegistryHit:
    """One organisation as a register has it."""

    registry: str
    name: str
    number: str
    status: str | None = None
    postcode: str | None = None
    kind: str | None = None
    source_url: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "registry": self.registry,
            "name": self.name,
            "number": self.number,
            "status": self.status,
            "postcode": self.postcode,
            "kind": self.kind,
            "source_url": self.source_url,
            **self.extra,
        }


@dataclass
class RegistryAnswer:
    hits: list[RegistryHit] = field(default_factory=list)
    note: str | None = None
    url: str | None = None

    def as_dict(self) -> dict:
        out: dict = {"results": [h.as_dict() for h in self.hits]}
        if self.note:
            out["note"] = self.note
        if self.url:
            out["open_this"] = self.url
        return out


def _get_json(url: str, headers: dict | None = None) -> tuple[dict | None, str | None]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------- NHS (free)


def nhs_ods_search(name: str, limit: int = MAX_RESULTS) -> RegistryAnswer:
    """Search the NHS organisation directory. No key, no charge.

    Returns live and dissolved entities both, with their status, because telling them
    apart is most of the value: an NHS trust that merged keeps its old name in every
    aggregator long after the register has retired it.
    """
    query = urllib.parse.urlencode({"Name": name, "Limit": max(1, limit)})
    data, error = _get_json(f"{ODS_BASE}?{query}")
    if error is not None:
        return RegistryAnswer(note=f"NHS directory unavailable ({error}).")

    hits = []
    for org in (data or {}).get("Organisations", [])[:limit]:
        code = org.get("OrgId", "")
        hits.append(
            RegistryHit(
                registry="NHS ODS",
                name=org.get("Name", ""),
                number=code,
                status=org.get("Status"),
                postcode=org.get("PostCode"),
                kind=org.get("PrimaryRoleDescription"),
                source_url=org.get("OrgLink") or f"{ODS_BASE}/{code}",
            )
        )
    if not hits:
        return RegistryAnswer(note=f"No NHS organisation found for {name!r}.")
    return RegistryAnswer(hits=hits)


# -------------------------------------------------------- Companies House (key)


def companies_house_key() -> str | None:
    raw = (os.getenv("COMPANIES_HOUSE_API_KEY") or "").strip()
    return raw or None


def companies_house_search(name: str, limit: int = MAX_RESULTS) -> RegistryAnswer:
    """Search Companies House. Uses the API when a key is set.

    Without a key this returns the public search URL instead of parsing the search
    page. Scraping a results page for a registration number is exactly the kind of
    brittle inference this project exists to avoid -- better to hand the agent the URL
    and let it read the page it cites.
    """
    key = companies_house_key()
    public = f"{COMPANIES_HOUSE_PUBLIC}/search/companies?q={urllib.parse.quote(name)}"
    if key is None:
        return RegistryAnswer(
            note=(
                "No COMPANIES_HOUSE_API_KEY set, so the register was not queried "
                "directly. Open the search page and read the result."
            ),
            url=public,
        )

    import base64

    token = base64.b64encode(f"{key}:".encode()).decode()
    query = urllib.parse.urlencode({"q": name, "items_per_page": max(1, limit)})
    data, error = _get_json(
        f"{COMPANIES_HOUSE_API}/search/companies?{query}",
        headers={"Authorization": f"Basic {token}"},
    )
    if error is not None:
        return RegistryAnswer(
            note=f"Companies House unavailable ({error}).", url=public
        )

    hits = []
    for item in (data or {}).get("items", [])[:limit]:
        number = item.get("company_number", "")
        address = item.get("address", {}) or {}
        hits.append(
            RegistryHit(
                registry="Companies House",
                name=item.get("title", ""),
                number=number,
                status=item.get("company_status"),
                postcode=address.get("postal_code"),
                kind=item.get("company_type"),
                source_url=f"{COMPANIES_HOUSE_PUBLIC}/company/{number}",
                extra={"incorporated_on": item.get("date_of_creation")},
            )
        )
    if not hits:
        return RegistryAnswer(note=f"No company found for {name!r}.", url=public)
    return RegistryAnswer(hits=hits)


# ------------------------------------------------------------------- the router


def is_nhs(domain: str | None, name: str | None) -> bool:
    haystack = f"{domain or ''} {name or ''}".lower()
    return ".nhs.uk" in haystack or "nhs " in haystack or haystack.endswith(" nhs")


def lookup(name: str, domain: str | None = None) -> RegistryAnswer:
    """Ask the register that covers this organisation.

    The routing is deliberately shallow. Getting it wrong costs one wasted free call;
    guessing a registration number from the wrong register would cost a wrong fact.
    """
    if is_nhs(domain, name):
        answer = nhs_ods_search(name)
        if answer.hits:
            return answer
        # An NHS-looking name with no directory entry may still be a limited company,
        # such as a private provider trading into the health service.
        fallback = companies_house_search(name)
        if fallback.hits or fallback.url:
            fallback.note = (
                (answer.note or "") + " " + (fallback.note or "")
            ).strip()
            return fallback
        return answer
    return companies_house_search(name)
