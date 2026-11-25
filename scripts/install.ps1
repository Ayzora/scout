# Install Scout on Windows. Safe to run more than once.
#
# The bash installer is for macOS and Linux; this is the same sequence with the
# PowerShell uv installer, because uv's shell script cannot run here.

$ErrorActionPreference = "Stop"

Set-Location (Join-Path $PSScriptRoot "..")
$root = (Get-Location).Path

function Say  ($m) { Write-Host "`n$m" -ForegroundColor White }
function Note ($m) { Write-Host "  $m" }
function Die  ($m) { Write-Host "`n$m`n" -ForegroundColor Red; exit 1 }

Say "Scout"
Note $root

# ------------------------------------------------------------------------ uv
# uv manages the Python version too, so whatever Python is on this machine is
# never involved.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "Installing uv"
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Die "Could not install uv. Install it by hand: https://docs.astral.sh/uv/"
    }
    foreach ($dir in @("$env:USERPROFILE\.local\bin", "$env:LOCALAPPDATA\Programs\uv")) {
        if (Test-Path $dir) { $env:PATH = "$dir;$env:PATH" }
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Die "uv installed but not on PATH. Open a new terminal and run this again."
    }
}
Note "uv $((uv --version) -split ' ' | Select-Object -Last 1)"

# ------------------------------------------------------------------ packages
Say "Installing Python and the packages"
uv sync --extra dev
if ($LASTEXITCODE -ne 0) { Die "uv sync failed." }

# ------------------------------------------------------------------- browser
# One Chromium, downloaded once, used by the browser engine and by verification.
Say "Installing Chromium for Playwright"
uv run playwright install chromium
if ($LASTEXITCODE -ne 0) { Die "Could not install Chromium." }

# -------------------------------------------------------------------- .env
if (-not (Test-Path ".env")) {
    Say "Creating .env"
    Copy-Item ".env.example" ".env"
    Note "Add your Anthropic API key to $root\.env"
} else {
    Note ".env already exists, leaving it alone"
}

# ---------------------------------------------------------------- database
Say "Preparing the database"
uv run scout init
if ($LASTEXITCODE -ne 0) { Die "Could not create the database." }

Say "Done"
@"
  Add your key:      ANTHROPIC_API_KEY=sk-ant-...   in .env
  Check the setup:   uv run scout doctor
  Open the app:      uv run scout serve
  Or research one:   uv run scout research -d example.com --objectives "who runs it"

  Nothing is sent anywhere except the research itself, and the only cost is
  the Anthropic API. A company runs about 2 minutes and 4 pence.
"@ | Write-Host
