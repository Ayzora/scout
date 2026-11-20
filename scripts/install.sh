#!/usr/bin/env bash
# Install Scout. Safe to run more than once.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n\n' "$*" >&2; exit 1; }

say "Scout"
note "$ROOT"

# ---------------------------------------------------------------------- uv
# uv manages the Python version too, so the system Python is never involved --
# which matters, because macOS still ships 3.9 and Scout needs 3.12.
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  else
    die "Neither uv nor curl found. Install uv: https://docs.astral.sh/uv/"
  fi
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -d "$candidate" ] && export PATH="$candidate:$PATH"
  done
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH. Open a new shell and run this again."
fi
note "uv $(uv --version | awk '{print $2}')"

# ----------------------------------------------------------------- packages
say "Installing Python $(grep -o '>=3\.[0-9]*' pyproject.toml | head -1 | tr -d '>=') and the packages"
uv sync --extra dev

# ------------------------------------------------------------------ browser
# One Chromium, downloaded once, used by the browser engine and by verification.
say "Installing Chromium for Playwright"
uv run playwright install chromium

# ----------------------------------------------------------------- the .env
if [ ! -f .env ]; then
  say "Creating .env"
  cp .env.example .env
  note "Add your Anthropic API key to $ROOT/.env"
else
  note ".env already exists, leaving it alone"
fi

# -------------------------------------------------------------- the database
say "Preparing the database"
uv run scout init

# ---------------------------------------------------------------------- done
say "Done"
cat <<'NEXT'
  Add your key:      ANTHROPIC_API_KEY=sk-ant-...   in .env
  Check the setup:   uv run scout doctor
  Open the app:      uv run scout serve
  Or research one:   uv run scout research -d example.com \
                       --objectives "who runs it and what they sell"

  Nothing is sent anywhere except the research itself, and the only cost is
  the Anthropic API. A company runs about 2 minutes and 4 pence.
NEXT
