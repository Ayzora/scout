"""branches, source tiers, step branch_id

Makes a run a tree rather than one flat conversation:

* ``branches`` is the tree itself -- one row per question researched in its own
  conversation, with its own token and cost totals.
* ``evidence.source_tier`` records how much weight a source carries, so corroboration
  can require two *independent* sources rather than two mentions.
* ``steps.branch_id`` keeps the agent trace attributable once several branches are
  interleaved.

Hand-edited after autogenerate: the generated foreign key was unnamed, which renders a
``drop_constraint(None)`` in the downgrade and fails. SQLite cannot drop an unnamed
constraint, so it is named here.

Revision ID: 4ac213b4936a
Revises: 40c19b51c09e
Create Date: 2026-11-19 13:00:03.716283
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4ac213b4936a"
down_revision: Union[str, Sequence[str], None] = "40c19b51c09e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK_STEPS_BRANCH = "fk_steps_branch_id"


def upgrade() -> None:
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_run_id", sa.Integer(), nullable=False),
        sa.Column("parent_branch_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("web_searches", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("depth >= 1", name="ck_branches_depth"),
        sa.ForeignKeyConstraint(
            ["company_run_id"], ["company_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_branch_id"], ["branches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("branches", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_branches_company_run_id"), ["company_run_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_branches_parent_branch_id"),
            ["parent_branch_id"],
            unique=False,
        )

    # Nullable, so the rows recorded before tiers existed stay valid and read as
    # "unknown tier" rather than being backfilled with a guess.
    with op.batch_alter_table("evidence", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_tier", sa.Integer(), nullable=True))

    # runs.engine was added by hand as nullable before Alembic existed; the model has
    # always declared it NOT NULL. Every existing row was backfilled to 'browser', so
    # this rebuild is safe -- and it is a rebuild, because SQLite cannot alter a column
    # in place.
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.alter_column(
            "engine",
            existing_type=sa.VARCHAR(length=16),
            nullable=False,
            existing_server_default=sa.text("'browser'"),
        )

    with op.batch_alter_table("steps", schema=None) as batch_op:
        batch_op.add_column(sa.Column("branch_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_steps_branch_id"), ["branch_id"], unique=False
        )
        batch_op.create_foreign_key(
            FK_STEPS_BRANCH, "branches", ["branch_id"], ["id"], ondelete="CASCADE"
        )


def downgrade() -> None:
    with op.batch_alter_table("steps", schema=None) as batch_op:
        batch_op.drop_constraint(FK_STEPS_BRANCH, type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_steps_branch_id"))
        batch_op.drop_column("branch_id")

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.alter_column(
            "engine",
            existing_type=sa.VARCHAR(length=16),
            nullable=True,
            existing_server_default=sa.text("'browser'"),
        )

    with op.batch_alter_table("evidence", schema=None) as batch_op:
        batch_op.drop_column("source_tier")

    with op.batch_alter_table("branches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_branches_parent_branch_id"))
        batch_op.drop_index(batch_op.f("ix_branches_company_run_id"))

    op.drop_table("branches")
