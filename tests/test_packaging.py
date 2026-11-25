"""The install has to survive leaving this directory.

A dashboard that works from a git checkout and not from an installed wheel is a
dashboard that works on one machine. These tests check the parts that silently
break when a file is not declared: the static files, the console script, and the
installer itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_console_script_points_at_the_cli(pyproject):
    assert pyproject["project"]["scripts"]["scout"] == "scout.cli:app"


def test_the_dashboard_files_are_declared_as_wheel_artifacts(pyproject):
    """Hatch ships .py files by default and nothing else. Without this the wheel
    installs cleanly and then serves a blank page."""
    artifacts = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"]
    assert any("static" in pattern for pattern in artifacts)


def test_the_dashboard_files_exist_where_the_server_looks(pyproject):
    from scout.api.app import STATIC_DIR

    assert (STATIC_DIR / "index.html").exists()
    assert (STATIC_DIR / "assets" / "app.css").exists()
    assert (STATIC_DIR / "assets" / "app.js").exists()


def test_the_page_only_asks_for_files_that_are_shipped():
    from scout.api.app import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for path in ("/assets/app.css", "/assets/app.js"):
        assert path in html
        assert (STATIC_DIR / path.lstrip("/")).exists()


def test_serving_needs_no_network_fonts_or_scripts():
    """An offline install must render the same as an online one.

    The XML namespace in an inline SVG is a URL that is never fetched, so the check
    is for resources the browser would actually load.
    """
    import re

    from scout.api.app import STATIC_DIR

    # A link the reader can click is fine; a resource the page pulls in is not.
    loads = re.compile(
        r"""<(?:link|script|img|iframe)\b[^>]*\b(?:href|src)\s*=\s*["']https?://"""
        r"""|@import\s+url\(\s*["']?https?://""",
        re.I,
    )
    for name in ("index.html", "assets/app.css", "assets/app.js"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8")
        assert not loads.search(text), f"{name} loads something over the network"
        for cdn in ("fonts.googleapis", "cdn.jsdelivr", "unpkg.com", "cdnjs."):
            assert cdn not in text, f"{name} depends on {cdn}"


def test_every_runtime_dependency_the_server_needs_is_declared(pyproject):
    declared = " ".join(pyproject["project"]["dependencies"])
    for package in ("fastapi", "uvicorn", "anthropic", "playwright", "sqlalchemy"):
        assert package in declared


def test_there_is_an_installer_for_windows():
    """install.sh cannot run on Windows: uv's shell installer is Unix-only."""
    script = ROOT / "scripts" / "install.ps1"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "astral.sh/uv/install.ps1" in text, "must use uv's PowerShell installer"
    assert "playwright install chromium" in text
    assert "scout init" in text


def test_the_windows_installer_does_not_overwrite_an_existing_env():
    text = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert 'if (-not (Test-Path ".env"))' in text


def test_ci_covers_every_platform_the_readme_claims(workflows):
    """Linux-only CI cannot tell you whether macOS or Windows work."""
    matrix = workflows["test.yml"]["jobs"]["test"]["strategy"]["matrix"]["os"]
    assert set(matrix) == {"ubuntu-latest", "macos-latest", "windows-latest"}
    assert workflows["test.yml"]["jobs"]["test"]["strategy"]["fail-fast"] is False


def test_ci_runs_the_installers_on_a_machine_with_nothing_on_it(workflows):
    """The one thing a developer's laptop can never test is a clean machine."""
    job = workflows["test.yml"]["jobs"]["install-script"]
    assert set(job["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest", "macos-latest", "windows-latest"
    }
    run = " ".join(s.get("run", "") for s in job["steps"])
    assert "install.sh" in run and "install.ps1" in run
    # Installing uv first would mean the installer's own uv step never runs.
    assert not any("setup-uv" in str(s.get("uses", "")) for s in job["steps"])


def test_the_installer_is_executable_and_valid_shell():
    script = ROOT / "scripts" / "install.sh"
    assert script.exists()
    # Git on Windows does not carry the executable bit, and nothing there runs
    # install.sh anyway.
    if os.name != "nt":
        assert script.stat().st_mode & 0o111, "install.sh is not executable"
    # Not on Windows: the bash on PATH there is the WSL stub, which exits non-zero
    # with no output when no distribution is installed, and install.sh is not run on
    # that platform anyway.
    if os.name == "nt" or shutil.which("bash") is None:
        pytest.skip("install.sh is not run on this platform")
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, (
        f"bash -n exited {result.returncode}\n{result.stdout}\n{result.stderr}"
    )


def test_the_installer_does_not_overwrite_an_existing_env():
    """It is run more than once, and .env holds the user's key."""
    text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "if [ ! -f .env ]" in text


def test_the_cli_exposes_the_commands_the_readme_promises():
    from scout.cli import app

    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert {"init", "doctor", "config", "research", "serve", "export", "verify",
            "show"} <= names


# ------------------------------------------------------------------- the README


@pytest.fixture(scope="module")
def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_readme_only_promises_commands_that_exist(readme):
    import re

    from scout.cli import app

    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    promised = set(re.findall(r"scout ([a-z][a-z-]+)", readme)) - {"doctor"}
    unknown = {p for p in promised if p not in names}
    assert not unknown, f"the README promises commands that do not exist: {unknown}"


def test_the_readme_lists_the_real_columns(readme):
    """The column list is the product. It cannot drift from the schema."""
    from scout.profile import BY_NAME

    for name in BY_NAME:
        assert f"`{name}`" in readme, f"{name} is missing from the README"


def test_the_readme_documents_every_setting_that_changes_behaviour(readme):
    for setting in (
        "ANTHROPIC_API_KEY", "SCOUT_MODEL", "SCOUT_DEPTH", "SCOUT_ENGINE",
        "SCOUT_RESPECT_ROBOTS", "SCOUT_TASK_BUDGET_TOKENS",
    ):
        assert setting in readme


def test_every_colour_token_is_a_valid_hex_value():
    """A Cyrillic 'e' inside #dce1e2 parses as nothing and the rule is dropped
    silently, which is how a theme loses a third of its hairlines without anyone
    noticing. Pinned here."""
    import re

    from scout.api.app import STATIC_DIR

    css = (STATIC_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    for name, value in re.findall(r"(--[a-z-]+):\s*([^;]+);", css):
        value = value.strip()
        if not value.startswith("#"):
            continue
        assert re.fullmatch(r"#[0-9a-fA-F]{3,8}", value), f"{name} is {value!r}"


def test_the_stylesheet_has_no_characters_that_only_look_like_ascii():
    from scout.api.app import STATIC_DIR

    for line in (STATIC_DIR / "assets" / "app.css").read_text(encoding="utf-8").split("\n"):
        suspect = [c for c in line if 0x0400 <= ord(c) <= 0x04FF]
        assert not suspect, f"Cyrillic look-alike in: {line.strip()}"


# ------------------------------------------------------- continuous integration


@pytest.fixture(scope="module")
def workflows() -> dict:
    import yaml

    out = {}
    for f in (ROOT / ".github" / "workflows").glob("*.yml"):
        out[f.name] = yaml.safe_load(f.read_text(encoding="utf-8"))
    return out


def test_ci_runs_the_tests_and_installs_the_browser(workflows):
    """A large part of the suite drives Chromium. CI that skips the browser install
    reports green on a suite that never ran."""
    steps = workflows["test.yml"]["jobs"]["test"]["steps"]
    run = " ".join(s.get("run", "") for s in steps)
    assert "uv run pytest" in run
    assert "playwright install" in run and "chromium" in run


def test_the_release_runs_the_tests_before_publishing(workflows):
    steps = workflows["release.yml"]["jobs"]["release"]["steps"]
    run = " ".join(s.get("run", "") for s in steps)
    assert "uv run pytest" in run
    assert "uv build" in run


def test_the_release_checks_the_dashboard_is_in_the_wheel(workflows):
    """Package data is easy to drop and invisible until someone installs it."""
    steps = workflows["release.yml"]["jobs"]["release"]["steps"]
    run = " ".join(s.get("run", "") for s in steps)
    assert "scout/api/static/index.html" in run


def test_the_release_fires_on_a_version_tag(workflows):
    # PyYAML reads a bare `on:` key as the boolean True.
    triggers = workflows["release.yml"].get("on") or workflows["release.yml"][True]
    assert "v*" in triggers["push"]["tags"]


def test_the_changelog_covers_the_declared_version(pyproject):
    version = pyproject["project"]["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {version}" in changelog, f"CHANGELOG has no entry for {version}"


def test_the_source_never_reads_a_file_at_the_platform_default_encoding():
    """Windows defaults to cp1252. A file holding an em dash or a diamond then
    fails to read at all, on that platform only."""
    import re

    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for n, line in enumerate(text.split("\n"), 1):
            if re.search(r"\.(read|write)_text\((?![^)]*encoding=)", line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
            if re.search(r"(?<!url)open\((?![^)]*encoding=)[^)]*[\"']w?[rb]?[\"']", line):
                if "newline=" not in line and "rb" not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert not offenders, f"reads without an explicit encoding: {offenders}"


def test_shell_scripts_are_checked_out_with_unix_line_endings():
    """Git on Windows rewrites endings on checkout by default. A shell script with
    CRLF will not run at all: bash reads the carriage return as part of the
    interpreter path and reports a bad interpreter."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes

    for script in (ROOT / "scripts").glob("*.sh"):
        assert b"\r\n" not in script.read_bytes(), f"{script.name} has CRLF endings"
