# Scout

Autonomous web research and lead enrichment agent. Scout drives a **real browser** to
investigate companies, gathers evidence-backed business intelligence, and turns a list of
company names or domains into structured profiles.

Local-first: the browser, the agent and the database all run on your own machine, with
your own Anthropic API key. No account to create, no hosted service, no cloud database.

> **Status: Phase 1 (CLI vertical slice).** Research, evidence capture, SQLite persistence
> and CSV export work from the command line. The dashboard, batch concurrency and the
> evaluation harness are not built yet — see [Roadmap](#roadmap).

## Why a browser

Conventional scrapers fetch HTML and apply selectors. That breaks on most modern company
sites, because the content is not in the HTML: it arrives later, from an API call, and some
of it has no URL at all — a team list behind a "meet the team" button, a spec table behind
a tab.

Scout runs Playwright, so it reads the page *after* JavaScript has run, and it can click,
type and scroll like a person. The test suite demonstrates exactly this: `tests/` serves a
fixture site whose company facts are absent from the served markup, asserts that a plain
HTTP fetch cannot see them, and then asserts that Scout can.

## Install

```bash
./scripts/install.sh
```

That checks for [uv](https://docs.astral.sh/uv/), installs dependencies against a pinned
Python 3.12+, installs Chromium for Playwright, writes `.env`, and creates the database.
Then put your key in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Check it took:

```bash
uv run scout config
```

## Use

```bash
# One company
uv run scout research --domain monzo.com \
  --objectives "What do they sell, and who leads the company?"

# A batch from CSV (needs a 'domain' column; 'name' optional)
uv run scout research --csv examples/companies.csv

# Qualify against an ideal customer profile
uv run scout research --csv examples/companies.csv \
  --icp '{"size":"200-5000 employees","region":"UK or EU"}'

# Inspect one company's findings with their sources
uv run scout show --company-run 1

# Export
uv run scout export --run 1 -o results.csv
```

More in [examples/README.md](examples/README.md).

## How it works

```
scout research
      │
      ├─ Playwright Chromium (headed by default, so you can watch)
      │     browser_goto / snapshot / click / type / scroll / find_links
      │
      ├─ Claude (your key)  ── web_search, web_fetch for the cheap first pass
      │
      └─ SQLite  findings · evidence · people · agent trace · metrics
```

**Pages are read as a rendered snapshot**: text, with a stable `[ref=N]` handle on every
interactive element. The agent can only click a ref the snapshot actually showed it, which
beats CSS selectors because refs survive re-renders. Screenshots are written to disk on
every step for inspection, but only sent to the model when the agent asks — vision is for
when layout or imagery carries the information, such as partner logos or an infographic.

**Findings are recorded the moment they are observed**, while the page is still open, each
with its source URL and a supporting quote. This is the central design decision. A model
asked to produce its sources *after* the fact reconstructs them from memory and invents
plausible URLs; recording at the point of observation ties every claim to a page that was
genuinely loaded. Scout also checks each claimed source against the pages the session
actually opened, and flags any that it never visited.

**Every claim is labelled** as an observed fact, an inference, or an explicit unknown — in
the database schema, not merely in the prompt. A gap is recorded as an unknown with a
reason rather than left silently absent, so completeness figures mean something.

**Synthesis is a second pass** that sees only the recorded findings, never the live pages,
so it cannot introduce a claim that no page supported.

## Limits, by design

Scout respects `robots.txt`, and when a site serves a bot-protection challenge or returns
401/403/429 it records the gap and moves on. There is no CAPTCHA solving and no evasion. It
does not log in, use supplied cookies, or scrape LinkedIn.

For people, Scout records only what the company itself publishes, and never constructs an
email address from a pattern. See [docs/data-protection.md](docs/data-protection.md).

## Development

```bash
uv sync --extra dev
uv run pytest           # 32 tests, no API key required
```

The test suite deliberately needs no API key: the browser layer, the recording and
provenance rules, persistence and export are all tested against a local fixture site, so
a failure is a real bug rather than model behaviour.

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 1 | CLI vertical slice: browser tools, evidence recording, SQLite, CSV export | **done** |
| 2 | Batch concurrency, resume, per-company budget tuning | next |
| 3 | FastAPI + SSE, Next.js dashboard with live agent timeline | not started |
| 4 | Decision-maker depth, ICP scoring, human review write-back | partial (recording works; no review UI) |
| 5 | Evaluation harness and report, outreach drafts, demo | not started |

## Licence

MIT — see [LICENSE](LICENSE).
