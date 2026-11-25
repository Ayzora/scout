# Changelog

Notable changes, newest first. Versions follow [semantic versioning](https://semver.org).

## 0.2.0 — 2026-11-25

Windows support, and proof for the other two.

- `scripts/install.ps1` installs Scout on Windows. The bash installer could not: uv's
  shell script does not run there.
- The suite runs on Ubuntu, macOS and Windows on every push, and each installer runs on
  a clean machine. Before this, only Linux was tested and the rest was assumption.
- Line endings are pinned so a Windows clone does not rewrite `install.sh` to CRLF,
  which stops it running under WSL or Git Bash.
- Tests no longer read files at the platform's default encoding, which on Windows is
  cp1252 and fails on any file holding a dash or a diamond. The source already had this
  right; a test now keeps it that way.

Not covered: watching the browser work (`SCOUT_HEADED=true`) on Windows, because the
CI runners have no desktop session.

## 0.1.1 — 2026-11-25

The dashboard tests assumed an API key was configured, so they passed on a developer's
machine and timed out in CI, where the Start button is correctly disabled. The suite no
longer behaves differently depending on whose shell it ran in. No change to the tool
itself.

## 0.1.0 — 2026-11-25

First release. Scout turns a list of company domains into one row per company,
with the page and the sentence behind every claim.

### Research

- Plans each company into three or four short branch conversations rather than one
  long one, so depth costs linearly instead of quadratically.
- Queries statutory registers directly for legal identity, which is free and exact
  where a web search is billed per request.
- Reads pages over plain HTTP and escalates to a real browser only when the response
  is a bot challenge, an error, or a shell that renders client-side.
- Refuses to run without a goal: research with no objectives or ideal customer has
  no reason to prefer one fact over another.
- Scores each company against an ideal customer profile, with the criteria it matched
  and missed.

### Output

- Twenty-one fixed columns, the same for every company, each exported alongside the
  hosts it came from. Findings matching no column are kept rather than dropped.
- A claim backed by two independent sources, one of them a register or the company
  itself, is marked corroborated.
- People are recorded only where the company or a public register publishes them.

### Verification

- `scout verify` reopens every cited page and checks the stored quote is on it. No
  model is involved, so it is free and repeatable.
- Handles Markdown, rendered text and scripts written without spaces between words.

### Interface

- A local dashboard at `scout serve`: runs, live progress, the profile with sources,
  people, every claim with its quote, and the agent's trace.
- Plain HTML, CSS and JavaScript, so installing Scout needs no Node toolchain.

### Install

- `scripts/install.sh` installs uv, Python 3.12, the packages and one Chromium, and
  is safe to run twice.
- `scout doctor` reports what is missing and what to type.

### Known limits

- One company at a time: no concurrency and no resume.
- Corroboration is rare in practice — most claims rest on a single source, and this
  is reported rather than hidden.
- Revenue is usually missing; it lives in filed accounts rather than on a page.
- Verification catches a fabricated quote, not a real quote attached to the wrong
  field.
