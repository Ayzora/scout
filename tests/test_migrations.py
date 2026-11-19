"""Migrations run, round-trip, and preserve data.

`data/scout.db` holds the measured token counts every cost comparison in the plan is
argued from, so a migration that quietly loses or rewrites them would destroy the
project's evidence base. These tests migrate a *copy* and assert the numbers survive.

They also guard a failure that is worse than a crash: running a pragma on an already
open connection starts a transaction Alembic does not own, after which `alembic
upgrade` exits 0 having applied almost nothing and leaves the version unstamped. The
version assertions below are what catch that.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE = "40c19b51c09e"
HEAD = "566689b1ac88"


def _alembic(*args: str, data_dir: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={
            **__import__("os").environ,
            "SCOUT_DATA_DIR": str(data_dir),
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    )
    return result


def _query(db: Path, sql: str):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _version(db: Path) -> str | None:
    rows = _query(db, "select version_num from alembic_version")
    return rows[0][0] if rows else None


def _has_column(db: Path, table: str, column: str) -> bool:
    rows = _query(db, f"select count(*) from pragma_table_info('{table}') where name='{column}'")
    return rows[0][0] == 1


def _has_table(db: Path, table: str) -> bool:
    rows = _query(db, f"select count(*) from sqlite_master where name='{table}'")
    return rows[0][0] > 0


@pytest.fixture
def fresh_db(tmp_path):
    """An empty data dir migrated from nothing to head."""
    _alembic("upgrade", "head", data_dir=tmp_path)
    return tmp_path / "scout.db"


def test_migrating_from_empty_builds_the_whole_schema(fresh_db):
    assert _version(fresh_db) == HEAD
    for table in (
        "runs",
        "company_runs",
        "companies",
        "findings",
        "evidence",
        "people",
        "steps",
        "branches",
        "qualifications",
        "reviews",
    ):
        assert _has_table(fresh_db, table), f"missing {table}"
    assert _has_column(fresh_db, "evidence", "source_tier")
    assert _has_column(fresh_db, "steps", "branch_id")
    assert _has_column(fresh_db, "runs", "engine")


def test_upgrade_actually_stamps_the_version(fresh_db):
    """The specific regression: a clean exit that applied nothing.

    If the migration runs inside a transaction Alembic does not control, the DDL is
    discarded and the version is never written, while the command still exits 0.
    """
    assert _version(fresh_db) == HEAD, "upgrade exited 0 without stamping the version"


def test_downgrade_and_upgrade_round_trip(fresh_db):
    """Every revision must come apart and go back together.

    Walks all the way down to the baseline rather than a fixed number of steps, so
    adding a revision does not silently stop exercising the ones below it.
    """
    data_dir = fresh_db.parent

    _alembic("downgrade", BASELINE, data_dir=data_dir)
    assert _version(fresh_db) == BASELINE
    assert not _has_table(fresh_db, "branches")
    assert not _has_column(fresh_db, "evidence", "source_tier")
    assert not _has_column(fresh_db, "findings", "branch_id")
    # The baseline tables themselves must survive being downgraded onto.
    assert _has_table(fresh_db, "findings") and _has_table(fresh_db, "runs")

    _alembic("upgrade", "head", data_dir=data_dir)
    assert _version(fresh_db) == HEAD
    assert _has_table(fresh_db, "branches")
    assert _has_column(fresh_db, "findings", "branch_id")
    assert _has_column(fresh_db, "people", "branch_id")


def test_a_fresh_install_is_not_reported_as_out_of_date(tmp_path):
    """``scout init`` builds the tables with create_all, which Alembic cannot see.

    Without stamping, a brand-new install told the user its own schema predated
    migrations and to run a command it did not need.
    """
    from dataclasses import replace

    from scout.config import load_settings
    from scout.db.session import init_db, reset_engine, schema_is_current

    settings = replace(load_settings(), data_dir=tmp_path)
    reset_engine()
    init_db(settings)
    try:
        ok, message = schema_is_current(settings)
        assert ok, message
        assert _version(tmp_path / "scout.db") == HEAD
    finally:
        reset_engine()


@pytest.mark.skipif(
    not (PROJECT_ROOT / "data" / "scout.db").exists(),
    reason="no local database with measured runs to migrate",
)
def test_migrating_the_real_database_preserves_the_measured_runs(tmp_path):
    """Migrate a copy of the live database and assert nothing moved.

    Those four rows are the baseline for every cost claim in the plan.
    """
    source = PROJECT_ROOT / "data" / "scout.db"
    target = tmp_path / "scout.db"
    shutil.copy(source, target)

    counts_before = {
        table: _query(target, f"select count(*) from {table}")[0][0]
        for table in ("runs", "company_runs", "findings", "evidence", "people", "steps")
    }
    usage_before = _query(
        target,
        "select id, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, cost_usd from company_runs order by id",
    )
    assert usage_before, "expected measured runs in the local database"
    untiered_before = (
        _query(target, "select count(*) from evidence where source_tier is null")[0][0]
        if _has_column(target, "evidence", "source_tier")
        else counts_before["evidence"]
    )

    _alembic("upgrade", "head", data_dir=tmp_path)

    assert _version(target) == HEAD
    counts_after = {
        table: _query(target, f"select count(*) from {table}")[0][0]
        for table in counts_before
    }
    assert counts_after == counts_before
    usage_after = _query(
        target,
        "select id, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, cost_usd from company_runs order by id",
    )
    assert usage_after == usage_before

    # The new column exists, and migrating does not invent tiers for rows recorded
    # before tiers existed -- an untiered row must read as unknown, not as tier 3.
    assert _has_column(target, "evidence", "source_tier")
    untiered_after = _query(
        target, "select count(*) from evidence where source_tier is null"
    )[0][0]
    assert untiered_after == untiered_before, (
        "migrating changed how many evidence rows have a source tier"
    )
