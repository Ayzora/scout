"""branch attribution on findings and people

Records which branch produced each claim. Branches research independently, so without
this there is no way to see two of them covering the same ground -- the known cost of
splitting one conversation into several.

``SET NULL`` rather than ``CASCADE``: deleting a branch must not delete the evidence it
gathered, which belongs to the company run.

Hand-edited after autogenerate, which left the foreign keys unnamed; SQLite cannot drop
an unnamed constraint, so the downgrade would fail as rendered.

Revision ID: 566689b1ac88
Revises: 4ac213b4936a
Create Date: 2026-11-19 18:30:33.445532
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "566689b1ac88"
down_revision: Union[str, Sequence[str], None] = "4ac213b4936a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK_NAMES = {"findings": "fk_findings_branch_id", "people": "fk_people_branch_id"}


def upgrade() -> None:
    for table, fk_name in FK_NAMES.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("branch_id", sa.Integer(), nullable=True))
            batch_op.create_index(
                batch_op.f(f"ix_{table}_branch_id"), ["branch_id"], unique=False
            )
            batch_op.create_foreign_key(
                fk_name, "branches", ["branch_id"], ["id"], ondelete="SET NULL"
            )


def downgrade() -> None:
    for table, fk_name in reversed(list(FK_NAMES.items())):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(fk_name, type_="foreignkey")
            batch_op.drop_index(batch_op.f(f"ix_{table}_branch_id"))
            batch_op.drop_column("branch_id")
