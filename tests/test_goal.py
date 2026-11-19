"""A run has to be told what it is for.

Without a goal the planner is handed "None given", which is an instruction to be
aimless, and the agent spends its budget on whatever it meets first. A real run came
back with thirteen overlapping service descriptions and nobody who works there.

These tests pin the two halves: Scout refuses to start without a direction, and when
it is given one the verdict reaches the reader.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from scout.cli import _print_qualification, app
from scout.config import load_settings
from scout.db import models as m
from scout.db.session import init_db, reset_engine, session_scope

runner = CliRunner()


# ------------------------------------------------------------------- refusing


def test_a_run_with_no_goal_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["research", "-d", "example.com"])
    assert result.exit_code == 2
    assert "no goal" in result.output.lower()
    # And it says how to fix it, rather than only that it refused.
    assert "--objectives" in result.output
    assert "--icp" in result.output


def test_the_refusal_happens_before_anything_is_spent(tmp_path, monkeypatch):
    """No API key is needed to be told the run has no goal."""
    monkeypatch.setenv("SCOUT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["research", "-d", "example.com"])
    assert result.exit_code == 2
    assert "no goal" in result.output.lower()
    assert "api key" not in result.output.lower()


@pytest.mark.parametrize(
    "args",
    [
        ["--objectives", "who runs it"],
        ["--icp", '{"sector": "healthcare"}'],
        ["--exploratory"],
    ],
)
def test_a_run_with_a_direction_is_not_refused(tmp_path, monkeypatch, args):
    """It will fail later for want of a key -- what matters is it got past the gate."""
    monkeypatch.setenv("SCOUT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["research", "-d", "example.com", *args])
    assert "This run has no goal" not in result.output


def test_exploratory_says_what_it_costs_you(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["research", "-d", "example.com", "--exploratory"])
    # Rich wraps the line, so compare on collapsed whitespace.
    assert "no qualification score" in " ".join(result.output.lower().split())


def test_a_missing_domain_is_still_caught_first(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_DATA_DIR", str(tmp_path))
    result = runner.invoke(app, ["research", "--objectives", "anything"])
    assert result.exit_code == 2
    assert "--domain" in result.output


# ----------------------------------------------------------------- the verdict


def test_the_verdict_is_printed_with_its_reasons(capsys):
    _print_qualification(
        SimpleNamespace(
            score=82.0,
            tier="strong",
            rationale="UK healthcare provider above the size floor.",
            icp_match={"matched": ["sector", "size"], "missed": ["buying signal"]},
        )
    )
    out = capsys.readouterr().out
    assert "STRONG" in out
    assert "82/100" in out
    assert "sector" in out and "buying signal" in out


def test_a_run_without_an_icp_says_why_there_is_no_score(capsys):
    _print_qualification(None)
    out = capsys.readouterr().out.lower()
    assert "no qualification" in out
    assert "--icp" in out


def test_a_partial_verdict_does_not_crash(capsys):
    _print_qualification(
        SimpleNamespace(score=None, tier=None, rationale=None, icp_match=None)
    )
    assert "unknown" in capsys.readouterr().out.lower()


def test_the_icp_is_stored_on_the_run(tmp_path):
    """The eval groups by configuration, so the goal has to survive the run."""
    settings = replace(load_settings(), data_dir=tmp_path)
    reset_engine()
    init_db(settings)
    from scout.agent.loop import create_run

    icp = {"sector": "UK healthcare", "size": "1000+ staff"}
    run_id = create_run(
        settings,
        label="t",
        objectives="who runs it",
        requested_fields=["company_name"],
        icp=icp,
    )
    with session_scope(settings) as s:
        run = s.get(m.Run, run_id)
        assert run.objectives == "who runs it"
        assert run.icp == icp


def test_the_prompt_sends_named_people_to_record_person():
    """A measured regression. A branch found the entire board and wrote it into
    `record_findings` as `leader:Chief Executive`, leaving the people table empty,
    because the People section described the job without naming the tool."""
    from scout.agent.prompts import system_prompt

    for engine in ("browser", "fetch", "browser_toolset"):
        prompt = system_prompt(engine)
        assert "`record_person`" in prompt, engine
        assert "never in `record_findings`" in prompt, engine
