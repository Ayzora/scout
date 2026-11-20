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
