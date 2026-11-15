"""Backfill retained Screaming Frog Crawl Depth and Inlinks metrics.

Revision ID: 0016
Revises: 0015
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _object(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if not {"pages", "import_batches", "import_rows"}.issubset(tables):
        return
    if "crawl_data" not in {column["name"] for column in inspector.get_columns("pages")}:
        return

    rows = bind.execute(sa.text("""
        SELECT ir.page_id, ir.raw_data, ib.mapping
        FROM import_rows AS ir
        JOIN import_batches AS ib ON ib.id = ir.batch_id
        WHERE ib.source = 'screaming_frog'
          AND ir.page_id IS NOT NULL
          AND ir.validation_status IN ('imported', 'updated')
          AND ib.status LIKE 'completed%'
        ORDER BY ib.imported_at, ir.row_number
    """)).mappings()
    supplied_by_page: dict[str, dict] = {}
    for row in rows:
        raw_data = _object(row["raw_data"])
        mapping = _object(row["mapping"])
        supplied = supplied_by_page.setdefault(row["page_id"], {})
        for field in ("crawl_depth", "inlinks"):
            source_header = mapping.get(field)
            if source_header and source_header in raw_data:
                supplied[field] = raw_data.get(source_header)

    pages = sa.table("pages", sa.column("id", sa.String()), sa.column("crawl_data", sa.JSON()))
    for page_id, supplied in supplied_by_page.items():
        if not supplied:
            continue
        current = _object(bind.execute(sa.select(pages.c.crawl_data).where(pages.c.id == page_id)).scalar_one_or_none())
        current["recognized"] = {**_object(current.get("recognized")), **supplied}
        bind.execute(sa.update(pages).where(pages.c.id == page_id).values(crawl_data=current))


def downgrade():
    # Historical import evidence is retained. Removing backfilled keys would
    # also remove values written by later application versions.
    pass
