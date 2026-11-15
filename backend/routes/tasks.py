from __future__ import annotations

import calendar
import csv
import io
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from ..database import get_db
from ..models import Project, RoadmapPeriod, SEOActivity, SEOTask, SEOTaskHistory
from ..services.activity import add_activity

router = APIRouter(prefix="/api/projects/{project_id}/task-manager", tags=["task-manager"])
PRIORITIES = {"critical", "high", "medium", "low"}
STATUSES = {"to_do", "in_progress", "on_hold", "completed"}


class TaskPayload(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    category: str = "General"
    priority: str = "medium"
    status: str = "to_do"
    assigned_to: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    completion_date: date | None = None
    completion_notes: str | None = None
    roadmap_period_id: str | None = None
    scheduled_year: int | None = None
    scheduled_month: int | None = None
    scheduled_week: date | None = None
    related_urls: list[str] = Field(default_factory=list)
    supporting_links: list[dict[str, str]] = Field(default_factory=list)
    notes: str | None = None
    source_issue_key: str | None = None
    source_issue_type: str | None = None
    source_section: str | None = None
    source_url: str | None = None
    source_recommendation_id: str | None = None


class TaskPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    category: str | None = None
    priority: str | None = None
    status: str | None = None
    assigned_to: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    completion_date: date | None = None
    completion_notes: str | None = None
    roadmap_period_id: str | None = None
    scheduled_year: int | None = None
    scheduled_month: int | None = None
    scheduled_week: date | None = None
    related_urls: list[str] | None = None
    supporting_links: list[dict[str, str]] | None = None
    notes: str | None = None


class PeriodPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    year: int = Field(ge=2000, le=2200)
    start_month: int = Field(ge=1, le=12)
    end_month: int = Field(ge=1, le=12)
    position: int | None = None


class PeriodPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    start_month: int | None = Field(default=None, ge=1, le=12)
    end_month: int | None = Field(default=None, ge=1, le=12)
    position: int | None = None


def project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def task_or_404(db: Session, project_id: str, task_id: str) -> SEOTask:
    task = db.get(SEOTask, task_id)
    if not task or task.project_id != project_id:
        raise HTTPException(404, "Task not found")
    return task


def validate_task(values: dict[str, Any], db: Session, project_id: str) -> None:
    if values.get("priority") and values["priority"] not in PRIORITIES:
        raise HTTPException(422, "Priority must be critical, high, medium, or low")
    if values.get("status") and values["status"] not in STATUSES:
        raise HTTPException(422, "Status must be To Do, In Progress, On Hold, or Completed")
    month = values.get("scheduled_month")
    if month is not None and not 1 <= month <= 12:
        raise HTTPException(422, "Scheduled month must be between 1 and 12")
    week = values.get("scheduled_week")
    if week and week.weekday() != 0:
        raise HTTPException(422, "Scheduled weeks must start on Monday")
    period_id = values.get("roadmap_period_id")
    if period_id:
        period = db.get(RoadmapPeriod, period_id)
        if not period or period.project_id != project_id:
            raise HTTPException(422, "Roadmap period does not belong to this project")


def serialize_task(task: SEOTask) -> dict[str, Any]:
    return {column: getattr(task, column) for column in (
        "id", "project_id", "roadmap_period_id", "title", "description", "category", "priority", "status", "assigned_to",
        "start_date", "due_date", "completion_date", "completion_notes", "scheduled_year", "scheduled_month", "scheduled_week",
        "related_urls", "supporting_links", "notes", "source_issue_key", "source_issue_type", "source_section", "source_url",
        "source_recommendation_id", "verified_resolution", "verification_data", "created_at", "updated_at",
    )}


def snapshot(task: SEOTask) -> dict[str, Any]:
    data = serialize_task(task)
    return {key: (value.isoformat() if isinstance(value, (date, datetime)) else value) for key, value in data.items() if key not in {"created_at", "updated_at"}}


def record_event(db: Session, task: SEOTask, event_type: str, before: dict | None = None, notes: str | None = None) -> None:
    after = snapshot(task)
    db.add(SEOTaskHistory(project_id=task.project_id, task_id=task.id, event_type=event_type, before_data=before or {}, after_data=after, notes=notes))
    status = "completed" if task.status == "completed" else "changed" if event_type == "updated" else "in_progress"
    if event_type == "verified_resolved":
        status = "resolved"
    section = task.source_section if task.source_section in {"pages", "site_architecture", "on_page", "keyword", "performance"} else "pages"
    add_activity(db, task.project_id, event_key=f"task:{task.id}:{event_type}:{datetime.now(timezone.utc).isoformat()}", task_id=task.id,
        source="task_manager", section=section, action_type=f"task_{event_type}", status=status,
        title=task.title, notes=notes or task.description, url=task.source_url or (task.related_urls or [None])[0],
        issue_key=task.source_issue_key, issue_type=task.source_issue_type, before_data=before or {}, after_data=after,
        activity_metadata={"task_id": task.id, "task_status": task.status})


def ensure_periods(db: Session, project_id: str, year: int) -> list[RoadmapPeriod]:
    periods = list(db.scalars(select(RoadmapPeriod).where(RoadmapPeriod.project_id == project_id, RoadmapPeriod.year == year).order_by(RoadmapPeriod.position)))
    if not periods:
        for position, (name, start, end) in enumerate((("Q1", 1, 3), ("Q2", 4, 6), ("Q3", 7, 9), ("Q4", 10, 12)), 1):
            db.add(RoadmapPeriod(project_id=project_id, year=year, name=name, position=position, start_month=start, end_month=end))
        db.flush()
        periods = list(db.scalars(select(RoadmapPeriod).where(RoadmapPeriod.project_id == project_id, RoadmapPeriod.year == year).order_by(RoadmapPeriod.position)))
    return periods


def serialize_period(period: RoadmapPeriod) -> dict[str, Any]:
    return {"id": period.id, "name": period.name, "year": period.year, "position": period.position, "start_month": period.start_month, "end_month": period.end_month}


def monday_weeks(year: int, month: int) -> list[str]:
    return [date(year, month, day).isoformat() for day in range(1, calendar.monthrange(year, month)[1] + 1) if date(year, month, day).weekday() == 0]


def week_monday(value: date) -> date:
    return value.fromordinal(value.toordinal() - value.weekday())


def selected_roadmap(db: Session, project_id: str, year: int, period_ids: list[str], statuses: list[str], priorities: list[str]) -> tuple[list[RoadmapPeriod], list[SEOTask]]:
    periods = ensure_periods(db, project_id, year)
    if period_ids:
        requested = set(period_ids)
        periods = [period for period in periods if period.id in requested]
        if len(periods) != len(requested):
            raise HTTPException(422, "One or more selected roadmap periods are invalid")
    statement = select(SEOTask).where(SEOTask.project_id == project_id, or_(SEOTask.scheduled_year == year, SEOTask.scheduled_year.is_(None)))
    if period_ids:
        statement = statement.where(SEOTask.roadmap_period_id.in_(period_ids))
    if statuses:
        invalid = set(statuses) - STATUSES
        if invalid: raise HTTPException(422, f"Invalid task status: {', '.join(sorted(invalid))}")
        statement = statement.where(SEOTask.status.in_(statuses))
    if priorities:
        invalid = set(priorities) - PRIORITIES
        if invalid: raise HTTPException(422, f"Invalid task priority: {', '.join(sorted(invalid))}")
        statement = statement.where(SEOTask.priority.in_(priorities))
    tasks = list(db.scalars(statement.order_by(SEOTask.due_date.is_(None), SEOTask.due_date, SEOTask.created_at)))
    return periods, tasks


def period_weeks(periods: list[RoadmapPeriod], year: int) -> list[date]:
    months = sorted({month for period in periods for month in range(period.start_month, period.end_month + 1)})
    return [date.fromisoformat(value) for month in months for value in monday_weeks(year, month)]


def task_span(task: SEOTask, weeks: list[date]) -> tuple[int, int] | None:
    if not weeks: return None
    start = task.start_date or task.scheduled_week
    if not start and task.scheduled_month:
        start = next((week for week in weeks if week.month == task.scheduled_month), None)
    if not start: return None
    end = task.due_date or start
    start_week, end_week = week_monday(start), week_monday(max(start, end))
    indexes = [index for index, week in enumerate(weeks) if start_week <= week <= end_week]
    if not indexes: return None
    return indexes[0], indexes[-1]


def roadmap_rows(tasks: list[SEOTask], periods: list[RoadmapPeriod]) -> list[dict[str, Any]]:
    period_names = {period.id: period.name for period in periods}
    return [{"Task": task.title, "Assigned Owner": task.assigned_to or "", "Status": task.status, "Priority": task.priority,
        "Start Date": task.start_date or "", "Due Date": task.due_date or "", "Quarter / Period": period_names.get(task.roadmap_period_id, "Unscheduled"),
        "Month": calendar.month_name[task.scheduled_month] if task.scheduled_month else "", "Scheduled Week": task.scheduled_week or "",
        "Category": task.category, "Related URLs": " | ".join(task.related_urls or []), "Verified Resolution": task.verified_resolution} for task in tasks]


def fit_text(value: str, width: float, font: str = "Helvetica", size: float = 7) -> str:
    if stringWidth(value, font, size) <= width: return value
    while value and stringWidth(f"{value}…", font, size) > width: value = value[:-1]
    return f"{value}…"


def roadmap_pdf(project: Project, year: int, periods: list[RoadmapPeriod], tasks: list[SEOTask]) -> bytes:
    output = io.BytesIO(); page_size = landscape(A3); width, height = page_size
    pdf = canvas.Canvas(output, pagesize=page_size, pageCompression=1)
    weeks = period_weeks(periods, year)
    week_chunks = [weeks[index:index + 14] for index in range(0, len(weeks), 14)] or [[]]
    task_chunks = [tasks[index:index + 28] for index in range(0, len(tasks), 28)] or [[]]
    colors = {"to_do": HexColor("#718079"), "in_progress": HexColor("#39725e"), "on_hold": HexColor("#b47b34"), "completed": HexColor("#4f8a68")}
    priority_colors = {"critical": HexColor("#bd4a42"), "high": HexColor("#d17b35"), "medium": HexColor("#b49a35"), "low": HexColor("#6c9183")}
    period_names = {period.id: period.name for period in periods}
    page_number = 0
    for task_page, task_chunk in enumerate(task_chunks, 1):
        for week_page, week_chunk in enumerate(week_chunks, 1):
            page_number += 1; left = 30; label_width = 250; grid_left = left + label_width; top = height - 34; row_height = 21
            week_width = (width - grid_left - 30) / max(1, len(week_chunk))
            pdf.setFillColor(HexColor("#24483c")); pdf.setFont("Helvetica-Bold", 16); pdf.drawString(left, top, f"{project.name} — SEO Roadmap {year}")
            pdf.setFillColor(HexColor("#66756f")); pdf.setFont("Helvetica", 7); pdf.drawRightString(width - 30, top, f"Timeline {week_page}/{len(week_chunks)} · Tasks {task_page}/{len(task_chunks)} · Page {page_number}")
            header_top = top - 22; pdf.setFillColor(HexColor("#e8f0ec")); pdf.rect(left, header_top - 49, width - 60, 50, fill=1, stroke=0)
            pdf.setFillColor(HexColor("#284d40")); pdf.setFont("Helvetica-Bold", 8); pdf.drawString(left + 6, header_top - 29, "TASK / OWNER")
            period_labels = [" / ".join(period.name for period in periods if period.start_month <= week.month <= period.end_month) for week in week_chunk]
            month_labels = [calendar.month_abbr[week.month] for week in week_chunk]
            for labels, y_position, font, font_size in ((period_labels, header_top - 10, "Helvetica-Bold", 6.5), (month_labels, header_top - 23, "Helvetica", 6.5)):
                group_start = 0
                for group_end in range(1, len(labels) + 1):
                    if group_end == len(labels) or labels[group_end] != labels[group_start]:
                        group_width = (group_end - group_start) * week_width
                        center = grid_left + group_start * week_width + group_width / 2
                        pdf.setFont(font, font_size); pdf.drawCentredString(center, y_position, fit_text(labels[group_start], group_width - 4, font, font_size))
                        pdf.setStrokeColor(HexColor("#b9ccc3")); pdf.line(grid_left + group_end * week_width, header_top, grid_left + group_end * week_width, header_top - 27)
                        group_start = group_end
            for index, week in enumerate(week_chunk):
                x = grid_left + index * week_width
                pdf.setFont("Helvetica-Bold", 7); pdf.drawCentredString(x + week_width / 2, header_top - 39, f"{week.day}-{calendar.month_abbr[week.month]}")
                pdf.setStrokeColor(HexColor("#d3ddd8")); pdf.line(x, header_top - 49, x, 42)
            y = header_top - 66
            chunk_indexes = {week: index for index, week in enumerate(week_chunk)}
            for task in task_chunk:
                pdf.setFillColor(HexColor("#ffffff") if int((header_top - y) / row_height) % 2 else HexColor("#f7f9f8")); pdf.rect(left, y - 5, width - 60, row_height, fill=1, stroke=0)
                pdf.setFillColor(priority_colors.get(task.priority, HexColor("#718079"))); pdf.circle(left + 5, y + 5, 2.5, fill=1, stroke=0)
                pdf.setFillColor(HexColor("#273f37")); pdf.setFont("Helvetica-Bold", 7); pdf.drawString(left + 12, y + 7, fit_text(task.title, label_width - 20, "Helvetica-Bold", 7))
                owner = task.assigned_to or "Unassigned"; period = period_names.get(task.roadmap_period_id, "Unscheduled")
                pdf.setFillColor(HexColor("#718079")); pdf.setFont("Helvetica", 6); pdf.drawString(left + 12, y - 1, fit_text(f"{owner} · {period} · {task.status.replace('_', ' ').title()}", label_width - 20, "Helvetica", 6))
                full_span = task_span(task, weeks)
                if full_span and week_chunk:
                    visible = [chunk_indexes[week] for week in week_chunk if full_span[0] <= weeks.index(week) <= full_span[1]]
                    if visible:
                        start, end = min(visible), max(visible); bar_x = grid_left + start * week_width + 2; bar_width = (end - start + 1) * week_width - 4
                        pdf.setFillColor(colors.get(task.status, colors["to_do"])); pdf.roundRect(bar_x, y, bar_width, 10, 3, fill=1, stroke=0)
                        if bar_width > 34: pdf.setFillColor(white); pdf.setFont("Helvetica-Bold", 5.5); pdf.drawString(bar_x + 4, y + 3, fit_text(task.status.replace("_", " ").title(), bar_width - 8, "Helvetica-Bold", 5.5))
                y -= row_height
            pdf.setStrokeColor(HexColor("#cbd7d1")); pdf.rect(left, header_top - 49, width - 60, 50 + row_height * len(task_chunk), fill=0, stroke=1)
            pdf.setFillColor(HexColor("#718079")); pdf.setFont("Helvetica", 6); pdf.drawString(left, 24, "Generated locally by SEO Data Workspace · Landscape timeline · Completion does not imply crawl verification")
            pdf.showPage()
    pdf.save(); return output.getvalue()


@router.get("/tasks")
def list_tasks(project_id: str, search: str = "", status: list[str] = Query(default=[]), priority: list[str] = Query(default=[]), category: list[str] = Query(default=[]), assigned_to: str = "", year: int | None = None, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    statement = select(SEOTask).where(SEOTask.project_id == project_id)
    if search:
        needle = f"%{search}%"
        statement = statement.where(or_(SEOTask.title.ilike(needle), SEOTask.description.ilike(needle), SEOTask.notes.ilike(needle), SEOTask.source_url.ilike(needle)))
    if status: statement = statement.where(SEOTask.status.in_(status))
    if priority: statement = statement.where(SEOTask.priority.in_(priority))
    if category: statement = statement.where(SEOTask.category.in_(category))
    if assigned_to: statement = statement.where(SEOTask.assigned_to.ilike(f"%{assigned_to}%"))
    if year: statement = statement.where(SEOTask.scheduled_year == year)
    items = list(db.scalars(statement.order_by(SEOTask.due_date.is_(None), SEOTask.due_date, SEOTask.created_at.desc())))
    today = date.today()
    counts = {key: sum(1 for task in items if task.status == key) for key in STATUSES}
    return {"items": [serialize_task(item) for item in items], "total": len(items), "counts": counts,
        "overdue": sum(1 for task in items if task.due_date and task.due_date < today and task.status != "completed"),
        "categories": sorted({task.category for task in db.scalars(select(SEOTask).where(SEOTask.project_id == project_id))})}


@router.post("/tasks", status_code=201)
def create_task(project_id: str, payload: TaskPayload, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    values = payload.model_dump()
    validate_task(values, db, project_id)
    if payload.source_issue_key:
        existing = db.scalar(select(SEOTask).where(SEOTask.project_id == project_id, SEOTask.source_issue_key == payload.source_issue_key, SEOTask.status != "completed").order_by(SEOTask.created_at.desc()))
        if existing:
            return {**serialize_task(existing), "duplicate": True}
    if payload.source_recommendation_id:
        existing = db.scalar(select(SEOTask).where(SEOTask.project_id == project_id, SEOTask.source_recommendation_id == payload.source_recommendation_id, SEOTask.status != "completed").order_by(SEOTask.created_at.desc()))
        if existing:
            return {**serialize_task(existing), "duplicate": True}
    if payload.status == "completed" and not payload.completion_date:
        values["completion_date"] = date.today()
    task = SEOTask(project_id=project_id, **values)
    db.add(task); db.flush(); record_event(db, task, "created"); db.commit(); db.refresh(task)
    return serialize_task(task)


@router.get("/tasks/{task_id}")
def task_detail(project_id: str, task_id: str, db: Session = Depends(get_db)):
    task = task_or_404(db, project_id, task_id)
    history = list(db.scalars(select(SEOTaskHistory).where(SEOTaskHistory.task_id == task.id).order_by(SEOTaskHistory.created_at.desc())))
    activities = list(db.scalars(select(SEOActivity).where(SEOActivity.task_id == task.id).order_by(SEOActivity.occurred_at.desc())))
    return {**serialize_task(task), "history": [{"id": item.id, "event_type": item.event_type, "before_data": item.before_data, "after_data": item.after_data, "notes": item.notes, "created_at": item.created_at} for item in history],
        "tracker_activity_ids": [item.id for item in activities]}


@router.patch("/tasks/{task_id}")
def update_task(project_id: str, task_id: str, payload: TaskPatch, db: Session = Depends(get_db)):
    task = task_or_404(db, project_id, task_id)
    values = payload.model_dump(exclude_unset=True)
    validate_task(values, db, project_id)
    before = snapshot(task)
    if values.get("status") == "completed" and task.status != "completed" and "completion_date" not in values:
        values["completion_date"] = date.today()
    if values.get("status") and values["status"] != "completed" and task.status == "completed" and "completion_date" not in values:
        values["completion_date"] = None
    for key, value in values.items(): setattr(task, key, value)
    if snapshot(task) == before:
        return serialize_task(task)
    record_event(db, task, "updated", before); db.commit(); db.refresh(task)
    return serialize_task(task)


@router.post("/tasks/{task_id}/duplicate", status_code=201)
def duplicate_task(project_id: str, task_id: str, db: Session = Depends(get_db)):
    source = task_or_404(db, project_id, task_id)
    copied = snapshot(source)
    for key in ("id", "project_id", "completion_date", "completion_notes", "source_issue_key", "verified_resolution", "verification_data"):
        copied.pop(key, None)
    for key in ("start_date", "due_date", "scheduled_week"):
        if copied.get(key): copied[key] = date.fromisoformat(copied[key])
    copied.update(title=f"{source.title} (copy)", status="to_do")
    task = SEOTask(project_id=project_id, **copied); db.add(task); db.flush(); record_event(db, task, "created", notes=f"Duplicated from {source.title}"); db.commit(); db.refresh(task)
    return serialize_task(task)


@router.delete("/tasks/{task_id}")
def delete_task(project_id: str, task_id: str, db: Session = Depends(get_db)):
    task = task_or_404(db, project_id, task_id)
    db.execute(update(SEOActivity).where(SEOActivity.task_id == task.id).values(task_id=None))
    for item in db.scalars(select(SEOTaskHistory).where(SEOTaskHistory.task_id == task.id)):
        db.delete(item)
    db.delete(task); db.commit()
    return {"deleted": True}


@router.get("/roadmap")
def roadmap(project_id: str, year: int = date.today().year, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    periods = ensure_periods(db, project_id, year); db.commit()
    tasks = list(db.scalars(select(SEOTask).where(SEOTask.project_id == project_id, or_(SEOTask.scheduled_year == year, SEOTask.scheduled_year.is_(None))).order_by(SEOTask.scheduled_week, SEOTask.due_date)))
    return {"year": year, "periods": [{**serialize_period(period), "months": [{"month": month, "name": calendar.month_name[month], "weeks": monday_weeks(year, month)} for month in range(period.start_month, period.end_month + 1)]} for period in periods],
        "tasks": [serialize_task(task) for task in tasks], "summary": {"total": len(tasks), "completed": sum(task.status == "completed" for task in tasks), "in_progress": sum(task.status == "in_progress" for task in tasks), "overdue": sum(bool(task.due_date and task.due_date < date.today() and task.status != "completed") for task in tasks)}}


@router.get("/roadmap/export")
def export_roadmap(project_id: str, format: str = "csv", year: int = date.today().year, period_ids: list[str] = Query(default=[]), statuses: list[str] = Query(default=[]), priorities: list[str] = Query(default=[]), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    periods, tasks = selected_roadmap(db, project_id, year, period_ids, statuses, priorities)
    db.commit()
    if format.lower() == "pdf":
        return Response(roadmap_pdf(project, year, periods, tasks), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="seo-roadmap-{year}.pdf"'})
    if format.lower() != "csv":
        raise HTTPException(422, "Roadmap export format must be csv or pdf")
    rows = roadmap_rows(tasks, periods); output = io.StringIO()
    fields = list(rows[0].keys()) if rows else ["Task", "Assigned Owner", "Status", "Priority", "Start Date", "Due Date", "Quarter / Period", "Month", "Scheduled Week"]
    writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="seo-roadmap-{year}.csv"'})


@router.post("/periods", status_code=201)
def create_period(project_id: str, payload: PeriodPayload, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    if payload.end_month < payload.start_month: raise HTTPException(422, "End month cannot precede start month")
    position = payload.position or (db.scalar(select(func.max(RoadmapPeriod.position)).where(RoadmapPeriod.project_id == project_id, RoadmapPeriod.year == payload.year)) or 0) + 1
    period = RoadmapPeriod(project_id=project_id, **payload.model_dump(exclude={"position"}), position=position)
    db.add(period); db.commit(); db.refresh(period); return serialize_period(period)


@router.patch("/periods/{period_id}")
def update_period(project_id: str, period_id: str, payload: PeriodPatch, db: Session = Depends(get_db)):
    period = db.get(RoadmapPeriod, period_id)
    if not period or period.project_id != project_id: raise HTTPException(404, "Roadmap period not found")
    values = payload.model_dump(exclude_unset=True)
    start, end = values.get("start_month", period.start_month), values.get("end_month", period.end_month)
    if end < start: raise HTTPException(422, "End month cannot precede start month")
    for key, value in values.items(): setattr(period, key, value)
    db.commit(); db.refresh(period); return serialize_period(period)


@router.delete("/periods/{period_id}")
def delete_period(project_id: str, period_id: str, reassign_to: str | None = None, unschedule: bool = False, db: Session = Depends(get_db)):
    period = db.get(RoadmapPeriod, period_id)
    if not period or period.project_id != project_id: raise HTTPException(404, "Roadmap period not found")
    tasks = list(db.scalars(select(SEOTask).where(SEOTask.roadmap_period_id == period.id)))
    if tasks and not (reassign_to or unschedule): raise HTTPException(409, f"This period contains {len(tasks)} tasks. Reassign or unschedule them before deletion.")
    target = db.get(RoadmapPeriod, reassign_to) if reassign_to else None
    if reassign_to and (not target or target.project_id != project_id): raise HTTPException(422, "Reassignment period not found")
    for task in tasks:
        task.roadmap_period_id = target.id if target else None
        if target: task.scheduled_year = target.year
        elif unschedule: task.scheduled_year = task.scheduled_month = task.scheduled_week = None
    db.delete(period); db.commit(); return {"deleted": True, "tasks_updated": len(tasks)}


@router.get("/export")
def export_tasks(project_id: str, format: str = "csv", year: int | None = None, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    statement = select(SEOTask).where(SEOTask.project_id == project_id)
    if year: statement = statement.where(SEOTask.scheduled_year == year)
    tasks = list(db.scalars(statement.order_by(SEOTask.created_at)))
    rows = [{"Title": t.title, "Description": t.description or "", "Category": t.category, "Priority": t.priority, "Status": t.status, "Assigned To": t.assigned_to or "", "Start Date": t.start_date or "", "Due Date": t.due_date or "", "Completion Date": t.completion_date or "", "Year": t.scheduled_year or "", "Month": t.scheduled_month or "", "Week Starting": t.scheduled_week or "", "Related URLs": " | ".join(t.related_urls or []), "Source Issue": t.source_issue_type or "", "Verified Resolved": t.verified_resolution, "Notes": t.notes or ""} for t in tasks]
    if format.lower() == "xlsx":
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer: pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="Tasks")
        return Response(output.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="seo-tasks.xlsx"'})
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()) if rows else ["Title", "Status"]); writer.writeheader(); writer.writerows(rows)
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="seo-tasks.csv"'})
