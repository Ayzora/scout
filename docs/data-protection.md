# Data protection

Scout researches companies, and company research touches personal data: the names and
job titles of people who work there. In the UK and EU that data is regulated under the
GDPR, and this is a public repository, so the rules Scout follows are written down rather
than left to the operator's judgement.

## What Scout records about people

Only what the company itself publishes, on a page Scout actually opened:

- Name and job title, as published.
- A business email address or phone number **only** where that page publishes it.
- The URL of that page, and the wording that named them.

## What Scout does not do

- **No email-pattern guessing.** Scout never derives `first.last@company.com` from an
  observed pattern. An address Scout did not read on a page does not get stored. This is
  deliberate: a guessed address is unverified personal data that Scout itself created.
- **No personal contact details.** No personal mobile numbers, no home addresses, no
  private social accounts.
- **No third-party contact databases.** Scout reads the open web, not purchased lists.
- **No authenticated scraping.** No logins, no supplied cookies, no LinkedIn scraping.
- **No evasion.** If a site serves a bot challenge or returns 401/403/429, Scout records
  the gap as an unknown and moves on.

These are enforced in two places: the agent's system prompt (`src/scout/agent/prompts.py`)
and the `record_person` tool description (`src/scout/agent/tools.py`), which is what the
model actually reads at the moment it decides what to store.

## Lawful basis

Scout does not choose a lawful basis for you. If you process the output of a Scout run,
that is your processing and you are the controller. For B2B prospecting in the UK and EU,
legitimate interests is the basis usually relied on, and relying on it requires a
balancing assessment, a way for people to object, and transparency about where the data
came from. Scout's contribution to that is provenance: every stored claim carries the URL
and the quote it came from, so you can always answer "where did you get this?".

## Deletion and export

Everything lives in one local SQLite file (`data/scout.db` by default) and one screenshot
directory. There is no cloud copy.

```bash
# Everything held about one company, including sources
scout show --company-run <id>

# Delete one company's research. Findings, people, evidence, screenshots and the
# agent trace are removed with it by cascade.
sqlite3 data/scout.db "DELETE FROM company_runs WHERE id = <id>;"
```

A first-class `scout forget --domain <domain>` command is planned so this is not a manual
SQL step; until then the cascade above is the supported route.

## Retention

Scout does not expire data on its own. If you keep research results, set your own
retention period and delete past it.
