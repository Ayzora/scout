"""The install has to survive leaving this directory.

A dashboard that works from a git checkout and not from an installed wheel is a
dashboard that works on one machine. These tests check the parts that silently
break when a file is not declared: the static files, the console script, and the
installer itself.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


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

    html = (STATIC_DIR / "index.html").read_text()
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
        text = (STATIC_DIR / name).read_text()
        assert not loads.search(text), f"{name} loads something over the network"
        for cdn in ("fonts.googleapis", "cdn.jsdelivr", "unpkg.com", "cdnjs."):
            assert cdn not in text, f"{name} depends on {cdn}"


def test_every_runtime_dependency_the_server_needs_is_declared(pyproject):
    declared = " ".join(pyproject["project"]["dependencies"])
    for package in ("fastapi", "uvicorn", "anthropic", "playwright", "sqlalchemy"):
        assert package in declared


def test_the_installer_is_executable_and_valid_shell():
    script = ROOT / "scripts" / "install.sh"
    assert script.exists()
    assert script.stat().st_mode & 0o111, "install.sh is not executable"
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_the_installer_does_not_overwrite_an_existing_env():
    """It is run more than once, and .env holds the user's key."""
    text = (ROOT / "scripts" / "install.sh").read_text()
    assert "if [ ! -f .env ]" in text


def test_the_cli_exposes_the_commands_the_readme_promises():
    from scout.cli import app

    names = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert {"init", "doctor", "config", "research", "serve", "export", "verify",
            "show"} <= names


# ------------------------------------------------------------------- the README


@pytest.fixture(scope="module")
def readme() -> str:
    return (ROOT / "README.md").read_text()


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

    css = (STATIC_DIR / "assets" / "app.css").read_text()
    for name, value in re.findall(r"(--[a-z-]+):\s*([^;]+);", css):
        value = value.strip()
        if not value.startswith("#"):
            continue
        assert re.fullmatch(r"#[0-9a-fA-F]{3,8}", value), f"{name} is {value!r}"


def test_the_stylesheet_has_no_characters_that_only_look_like_ascii():
    from scout.api.app import STATIC_DIR

    for line in (STATIC_DIR / "assets" / "app.css").read_text().split("\n"):
        suspect = [c for c in line if 0x0400 <= ord(c) <= 0x04FF]
        assert not suspect, f"Cyrillic look-alike in: {line.strip()}"
