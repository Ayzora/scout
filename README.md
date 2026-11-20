# Scout

Autonomous web research and lead enrichment agent. Scout drives a **real browser** to
investigate companies, gathers evidence-backed business intelligence, and turns a list of
company names or domains into structured profiles.

Local-first: the browser, the agent and the database all run on your own machine, with
your own Anthropic API key. No account to create, no hosted service, no cloud database.

> **Status: works from the command line.** Research runs as a tree of branches, every
> quote can be checked against its source page, and one company measured **$0.087 for 69
> findings**. The dashboard, batch concurrency and the evaluation harness are not built
> yet — see [Roadmap](#roadmap).

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

# Choose how deep to go: shallow | standard | deep
uv run scout research --domain monzo.com --depth deep \
  --objectives "What do they sell, and who leads the company?"

# Inspect one company: findings, sources, the branch tree, what is corroborated
uv run scout show --company-run 1

# Check every recorded quote against the page it came from (free, no model)
uv run scout verify --run 1

# Export
uv run scout export --run 1 -o results.csv
```

More in [examples/README.md](examples/README.md).

## How it works

```
scout research
      │
      ├─ plan        one structured call, no tools: which branches to research
      │
      ├─ branches    each its own short conversation, own budget, own question
      │                identity · products · people · financials · market · signals
      │                  │
      │                  ├─ Playwright Chromium (headed by default, so you can watch)
      │                  └─ Claude (your key) + web_search / web_fetch
      │
      ├─ merge       pure Python: group equal claims, weigh sources, find conflicts
      │
      └─ synthesize  a profile from the recorded findings only
```

**Research is a tree, not one long conversation.** A single conversation re-reads its own
history every turn, so its cost grows with the *square* of its length: ten times the depth
costs forty times as much. Split across branches it grows linearly. That is the whole
reason depth is affordable — `--depth deep` costs less than a shallow pass did on a larger
model. Branches run one after another; running them at the same time would save time, not
money.

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

**Sources are weighed by where they come from**, not by assertion: a statutory register
above the company's own site, above the press, above anything openly editable. A claim
counts as corroborated only with two *different* hosts and at least one primary among
them — so two aggregators copying the same original do not confirm each other, and three
pages of one site are one source. A single-sourced claim is flagged, never discarded.

**Disagreements are surfaced** rather than resolved silently. If two sources give
different headquarters, both are recorded and the conflict is reported.

**Synthesis is a second pass** that sees only the recorded findings, never the live pages,
so it cannot introduce a claim that no page supported.

**And the quotes can be checked.** `scout verify` reopens every cited page and matches the
stored quote against the rendered text. It uses no model, so it costs nothing and can run
after every research run. On a real run: 45 verbatim, 2 paraphrased, 0 fabricated.

## The output is one row, with fixed columns

Every company exports the same columns — `legal_name`, `registration_number`,
`employee_count`, `revenue` and the rest — whatever the agent happened to call a field
while researching. Each column carries a second column saying which hosts it came from,
and a tick where two independent sources agree. Findings that fit no column are kept in
`extras_json` rather than dropped, so a gap in the vocabulary is visible in the output.

That matters more than it sounds. Before this, three runs on one company recorded the
same fact as `identifier_ods_code`, `registration_number (Companies House)` and
`total_headcount_2025_26_narrative`. Good reading, and nothing a spreadsheet could sort.

## Limits, by design

Scout respects `robots.txt` by default, and when a site serves a bot-protection challenge
or returns 401/403/429 it records the gap and moves on. There is no CAPTCHA solving and no
evasion. It does not log in, use supplied cookies, or scrape LinkedIn.

`SCOUT_RESPECT_ROBOTS=false` turns the robots check off. It changes nothing else — the
challenge detection, the refusal to log in and the rest of the limits above are not
conditional on it. Worth knowing before reaching for it: a site that allows crawling can
still block an unfamiliar client, and that is the firewall talking rather than a rule you
are bound by, so the switch is often not the thing you actually need.

For people, Scout records only what the company itself publishes, and never constructs an
email address from a pattern. See [docs/data-protection.md](docs/data-protection.md).

## Development

```bash
uv sync --extra dev
uv run pytest           # 242 tests, no API key required
uv run pytest -m live   # a handful that call free public APIs
```

The test suite deliberately needs no API key: the browser layer, the recording and
provenance rules, persistence and export are all tested against a local fixture site, so
a failure is a real bug rather than model behaviour.

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 1 | CLI vertical slice: browser tools, evidence recording, SQLite, CSV export | **done** |
| 1b | Cost: prompt caching, cheap-model default, `scout verify` | **done** |
| 1c | Deep research: planner, branches, source tiers, corroboration, conflicts | **done** |
| 2 | Batch concurrency and resume — which also parallelises branches | next |
| 3 | FastAPI + SSE, Next.js dashboard with live agent timeline | not started |
| 4 | ICP scoring and human review write-back | partial (recording works; no review UI) |
| 5 | Evaluation harness: the three engines and the model tiers, measured | not started |

### Engines

Scout can read the web three ways, all recording into the same schema so a run of each on
one company is directly comparable. Which is best is an open question the evaluation set is
meant to answer, not something asserted here.

| `SCOUT_ENGINE` | How it reads pages |
|---|---|
| `browser` | Playwright, through Scout's own seven tools. The default. |
| `fetch` | No browser at all: server-side `web_search` and `web_fetch` only, i.e. conventional scripted research. |
| `browser_toolset` | The same Playwright browser behind Anthropic's `browser_toolset_20260801` interface, to test whether its standard vocabulary navigates better than Scout's own. |

## Licence

MIT — see [LICENSE](LICENSE).
