"""Keep crawl evidence separate from manual page overrides.

Revision ID: 0013
Revises: 0012
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    page_columns = {column["name"] for column in inspector.get_columns("pages")}
    if "crawl_data" not in page_columns:
        op.add_column("pages", sa.Column("crawl_data", sa.JSON(), nullable=False, server_default="{}"))
    if "manual_overrides" not in page_columns:
        op.add_column("pages", sa.Column("manual_overrides", sa.JSON(), nullable=False, server_default="{}"))
    audit_columns = {column["name"] for column in inspector.get_columns("audit_cases")}
    if "priority_override" not in audit_columns:
        op.add_column("audit_cases", sa.Column("priority_override", sa.String(length=40), nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    audit_columns = {column["name"] for column in inspector.get_columns("audit_cases")}
    if "priority_override" in audit_columns:
        op.drop_column("audit_cases", "priority_override")
    page_columns = {column["name"] for column in inspector.get_columns("pages")}
    if "manual_overrides" in page_columns:
        op.drop_column("pages", "manual_overrides")
    if "crawl_data" in page_columns:
        op.drop_column("pages", "crawl_data")
