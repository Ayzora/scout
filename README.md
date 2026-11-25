# Scout

Give Scout a list of company domains. It researches each one, records every claim with
the page and the sentence it came from, and gives you back one row per company.

Everything runs on your machine: the browser, the agent, the database. You bring an
Anthropic API key. There is no account and no hosted service.

A company costs about **4p and takes about two minutes**.

```
legal_name             NORTHGATE HOLDINGS LTD.
trading_name           Northgate
registration_number    01234567            ◆ corroborated
legal_status           Private limited company (active)
headquarters_location  Unit 4, Lockhurst Lane, Coventry CV6 5NY
industry               SIC 32300 — Manufacture of sports goods
founded                Incorporated 1 November 2011
contact_email          hello@northgate.example
people                 4 directors, from the officer register
```

The brand and the registered company are often different names, and the domain tells
you neither. That gap is where most databases are wrong.

## What makes it different from asking a chatbot

For one company, nothing — ask Claude, it is faster. The difference shows up in four
places.

**It scales.** 500 companies is one CSV with the same columns in every row, about £12
and a night of wall-clock. Five hundred chat conversations is not a dataset.

**The quotes are checked by a machine.** Every claim stores the sentence it came from.
`scout verify` reopens each page and checks the sentence is still there. No model is
involved, so it is free and repeatable. Measured on three companies: 100%, 98%, 94%.

**It says what it could not find.** One run recorded twelve explicit unknowns, each with
a reason — "the site states 'a combined 100+ years of experience' but gives no
headcount". It does not turn that into an estimate.

**It is a database, not a transcript.** Re-verify a claim from three months ago. Diff
two runs on the same company. Export to a CRM. Close the tab and it is still there.

## Install

```bash
git clone https://github.com/Ayzora/scout.git
cd scout
./scripts/install.sh
```

The script installs [uv](https://docs.astral.sh/uv/) if you do not have it, uses it to
fetch Python 3.12 (macOS still ships 3.9, which is too old), installs the packages and
one Chromium, writes `.env`, and creates the database. Run it twice and it will not
overwrite anything.

Put your key in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Then check the install:

```bash
uv run scout doctor
```

It tells you what is missing and what to type.

## Use it

### The dashboard

```bash
uv run scout serve
```

Open http://127.0.0.1:8000. Start a run, watch it, read the results: the profile with a
source beside every value, the fit against what you are looking for, the people, every
claim with its quote, and a trace of what the agent did.

### The command line

```bash
uv run scout research -d northgate.example \
  --objectives "who runs it, how big, what it sells, what changed recently" \
  --icp '{"sector": "UK manufacturing", "size": "50+ staff", "buying_signal": "recent funding"}'
```

Several companies at once:

```bash
uv run scout research --csv leads.csv --objectives "..." --label "november"
```

`leads.csv` needs a `domain` column. A `name` column is optional.

Then, free — neither command calls the API:

```bash
uv run scout verify --run 7              # check every quote against its page
uv run scout show --company-run 7        # the profile, with sources
uv run scout export --run 7 --out out.csv
```

### Give it a goal

Scout refuses to run without `--objectives` or `--icp`. This is deliberate. With no
goal the planner has no reason to prefer one fact over another, and an early run came
back with thirteen overlapping service descriptions and nobody who worked there.

`--icp` is a JSON description of who you want. Each company is scored 0–100 against it,
with the criteria it matched and missed. Without one you get a description instead of a
verdict.

`--exploratory` skips the check if you really want an aimless run.

## What you get

Twenty-one fixed columns, the same for every company:

`legal_name` · `trading_name` · `website` · `registration_number` · `legal_status` ·
`regulator` · `headquarters_location` · `country` · `contact_email` · `contact_phone` ·
`industry` · `business_activities` · `products_services` · `employee_count` · `revenue` ·
`founded` · `ownership` · `geographic_presence` · `target_markets` ·
`technology_signals` · `recent_developments`

Each one exports with a second column saying which hosts it came from. A claim backed by
two independent sources, one of them a register or the company itself, is marked
corroborated. Anything that fits no column is kept in `extras_json` rather than dropped,
so a gap in the vocabulary is visible in the output.

People are separate, with their role and the page that published them.

## How it works

**A plan, then branches.** One cheap structured call picks three or four areas worth
researching. Each becomes its own short conversation with its own budget. This is not
tidiness — a single conversation re-reads its whole history every turn, so ten times the
depth costs forty times as much. Split across branches it costs ten.

**Registers first.** Legal identity comes from Companies House or the NHS organisation
directory through a direct lookup: free, exact, and stronger than anything a web search
returns. Searching is the one thing billed per request rather than per token, and on an
early run ten searches were 82% of the bill while all the browsing and reasoning came to
two pence.

**Pages are fetched, then escalated.** Plain HTTP first. If what comes back is a bot
challenge, an error, or a shell that only renders in a browser, the page is reopened in
Chromium automatically. The page decides, not a rule about the host — and the split is
not where you would guess. Registries, regulators and job boards serve clean HTML.
Company marketing sites are the JavaScript shells: one measured homepage held 479
characters of text inside 239KB of markup.

**Claims are recorded as they are seen**, with the URL and quote, while the page is
still open. A model asked to produce its sources afterwards invents plausible ones.

**Then they are merged.** Equal claims group, extra evidence attaches, single-sourced
claims are flagged, and two sources that genuinely disagree are both recorded. That step
is plain Python with no model in it, so the corroboration count is a fact about the
evidence rather than a judgement.

## Configuration

Everything lives in `.env`. `uv run scout config` prints what is in effect.

| | |
|---|---|
| `ANTHROPIC_API_KEY` | Required. Nothing that costs money runs without it. |
| `SCOUT_MODEL` | `claude-haiku-5-5`. Opus cost 34× more for identical measured work. |
| `SCOUT_DEPTH` | `shallow` (1 branch), `standard` (4), `deep` (6 plus follow-ups). |
| `SCOUT_ENGINE` | `browser`, `fetch`, or `browser_toolset`. |
| `SCOUT_TASK_BUDGET_TOKENS` | Per branch, not per run. Default 40,000. |
| `SCOUT_HEADED` | Show the browser window when one is actually needed. |
| `SCOUT_RESPECT_ROBOTS` | On by default. |
| `COMPANIES_HOUSE_API_KEY` | Optional and free. Without it Scout hands the agent the public search page instead of guessing a company number. |

## Limits

Scout respects `robots.txt` by default. When a site serves a bot challenge or returns
401, 403 or 429, it records the gap and moves on. There is no CAPTCHA solving and no
evasion. It does not log in, use supplied cookies, or scrape LinkedIn.

For people it records only what the company or a public register publishes, and never
builds an email address from a pattern. See [docs/data-protection.md](docs/data-protection.md).

Known weaknesses, measured rather than hidden:

- **Corroboration is rare** — 0 to 3% of claims across the runs so far. The machinery
  works; the agent only finds a second source when it happens to pass one. Chasing them
  deliberately would cost more per company, which is a product decision, not a bug.
- **Revenue is usually missing.** It lives in filed accounts as a PDF or iXBRL document,
  not on any page.
- **One company at a time.** No concurrency and no resume. 500 companies works but takes
  a night, and a crash at number 400 means re-running the rest by hand.
- **Verification catches a fabricated quote, not a misread one.** A real sentence
  attached to the wrong field passes.

## Development

```bash
uv sync --extra dev
uv run pytest           # 279 tests, no API key, no network
uv run pytest -m live   # a few that call free public APIs
```

The suite serves a fixture site whose content is only reachable after JavaScript runs,
so the browser path is tested for real rather than mocked. The dashboard is opened in
Chromium and clicked through, because nothing else type-checks a template string.

Database changes go through Alembic:

```bash
uv run alembic revision --autogenerate -m "what changed"
uv run alembic upgrade head
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## Licence

MIT.
