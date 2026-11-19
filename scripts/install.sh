#!/usr/bin/env bash
# Scout installer. Idempotent: safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

say() { printf '\n\033[1m%s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

say "1/5  Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
  fail "uv is not installed. Install it with:
  curl -LsSf https://astral.sh/uv/install.sh | sh
then re-run this script."
fi
echo "     uv $(uv --version | awk '{print $2}')"

say "2/5  Installing Python dependencies"
# uv fetches a suitable Python itself; the system Python is not used.
uv sync --extra dev

say "3/5  Installing the Chromium browser for Playwright"
uv run playwright install chromium

say "4/5  Creating configuration"
if [ -f .env ]; then
  echo "     .env already exists, leaving it alone"
else
  cp .env.example .env
  echo "     wrote .env from .env.example"
fi

say "5/5  Initialising the database"
uv run scout init

say "Done."
cat <<'NEXT'
Next steps:

  1. Put your Anthropic API key in .env
         ANTHROPIC_API_KEY=sk-ant-...
     You pay your own API usage. Check your configuration with:
         uv run scout config

  2. Research a company:
         uv run scout research --domain example.com \
           --objectives "What do they sell and who runs the company?"

  3. Export the results:
         uv run scout export --run 1 -o results.csv
NEXT
