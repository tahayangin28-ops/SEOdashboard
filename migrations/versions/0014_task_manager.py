"""Add the offline task manager and roadmap.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "roadmap_periods" not in tables:
        op.create_table("roadmap_periods",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("year", sa.Integer(), nullable=False), sa.Column("name", sa.String(120), nullable=False), sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("start_month", sa.Integer(), nullable=False), sa.Column("end_month", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_id", "year", "position", name="uq_roadmap_period_position"))
        op.create_index("ix_roadmap_periods_project_id", "roadmap_periods", ["project_id"])
        op.create_index("ix_roadmap_periods_year", "roadmap_periods", ["year"])
    if "seo_tasks" not in tables:
        op.create_table("seo_tasks",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("roadmap_period_id", sa.String(36), sa.ForeignKey("roadmap_periods.id")), sa.Column("title", sa.String(500), nullable=False),
            sa.Column("description", sa.Text()), sa.Column("category", sa.String(120), nullable=False), sa.Column("priority", sa.String(40), nullable=False),
            sa.Column("status", sa.String(40), nullable=False), sa.Column("assigned_to", sa.String(160)), sa.Column("start_date", sa.Date()),
            sa.Column("due_date", sa.Date()), sa.Column("completion_date", sa.Date()), sa.Column("completion_notes", sa.Text()),
            sa.Column("scheduled_year", sa.Integer()), sa.Column("scheduled_month", sa.Integer()), sa.Column("scheduled_week", sa.Date()),
            sa.Column("related_urls", sa.JSON(), nullable=False), sa.Column("supporting_links", sa.JSON(), nullable=False), sa.Column("notes", sa.Text()),
            sa.Column("source_issue_key", sa.String(2048)), sa.Column("source_issue_type", sa.String(255)), sa.Column("source_section", sa.String(40)),
            sa.Column("source_url", sa.String(4096)), sa.Column("source_recommendation_id", sa.String(36), sa.ForeignKey("page_metadata_recommendations.id")),
            sa.Column("verified_resolution", sa.Boolean(), nullable=False), sa.Column("verification_data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
        for column in ("project_id", "roadmap_period_id", "category", "priority", "status", "due_date", "scheduled_year", "source_issue_key", "source_section", "source_recommendation_id", "verified_resolution", "created_at"):
            op.create_index(f"ix_seo_tasks_{column}", "seo_tasks", [column])
    if "seo_task_history" not in tables:
        op.create_table("seo_task_history",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("task_id", sa.String(36), sa.ForeignKey("seo_tasks.id"), nullable=False), sa.Column("event_type", sa.String(80), nullable=False),
            sa.Column("before_data", sa.JSON(), nullable=False), sa.Column("after_data", sa.JSON(), nullable=False), sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        for column in ("project_id", "task_id", "event_type", "created_at"):
            op.create_index(f"ix_seo_task_history_{column}", "seo_task_history", [column])
    activity_columns = {column["name"] for column in inspector.get_columns("seo_activities")}
    if "task_id" not in activity_columns:
        op.add_column("seo_activities", sa.Column("task_id", sa.String(36), sa.ForeignKey("seo_tasks.id")))
        op.create_index("ix_seo_activities_task_id", "seo_activities", ["task_id"])


def downgrade():
    op.drop_index("ix_seo_activities_task_id", table_name="seo_activities")
    op.drop_column("seo_activities", "task_id")
    op.drop_table("seo_task_history")
    op.drop_table("seo_tasks")
    op.drop_table("roadmap_periods")
