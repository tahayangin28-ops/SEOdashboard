"""Persist Screaming Frog Inlinks snapshots and observations.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "internal_link_snapshots" not in tables:
        op.create_table("internal_link_snapshots",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("import_batch_id", sa.String(36), sa.ForeignKey("import_batches.id"), nullable=False), sa.Column("complete_snapshot", sa.Boolean(), nullable=False),
            sa.Column("internal_count", sa.Integer(), nullable=False), sa.Column("external_count", sa.Integer(), nullable=False), sa.Column("unmatched_count", sa.Integer(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("import_batch_id", name="uq_internal_link_snapshot_batch"))
        for column in ("project_id", "import_batch_id", "observed_at"):
            op.create_index(f"ix_internal_link_snapshots_{column}", "internal_link_snapshots", [column])
    if "internal_link_observations" not in tables:
        op.create_table("internal_link_observations",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("internal_link_snapshots.id"), nullable=False), sa.Column("import_batch_id", sa.String(36), sa.ForeignKey("import_batches.id"), nullable=False),
            sa.Column("row_number", sa.Integer(), nullable=False), sa.Column("source_page_id", sa.String(36), sa.ForeignKey("pages.id")), sa.Column("destination_page_id", sa.String(36), sa.ForeignKey("pages.id")),
            sa.Column("source_url", sa.String(4096), nullable=False), sa.Column("normalized_source_url", sa.String(4096)), sa.Column("destination_url", sa.String(4096), nullable=False), sa.Column("normalized_destination_url", sa.String(4096)),
            sa.Column("is_internal", sa.Boolean(), nullable=False), sa.Column("link_type", sa.String(120)), sa.Column("size_bytes", sa.Integer()), sa.Column("alt_text", sa.Text()), sa.Column("anchor", sa.Text()),
            sa.Column("status_code", sa.Integer()), sa.Column("status_text", sa.String(255)), sa.Column("follow", sa.String(80)), sa.Column("target", sa.String(255)), sa.Column("rel", sa.String(500)),
            sa.Column("path_type", sa.String(120)), sa.Column("link_path", sa.Text()), sa.Column("link_position", sa.String(120)), sa.Column("link_origin", sa.String(120)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("import_batch_id", "row_number", name="uq_internal_link_batch_row"))
        for column in ("project_id", "snapshot_id", "import_batch_id", "source_page_id", "destination_page_id", "normalized_source_url", "normalized_destination_url", "is_internal", "status_code", "link_position", "link_origin"):
            op.create_index(f"ix_internal_link_observations_{column}", "internal_link_observations", [column])


def downgrade():
    op.drop_table("internal_link_observations")
    op.drop_table("internal_link_snapshots")
