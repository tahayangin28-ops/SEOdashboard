from datetime import date
from types import SimpleNamespace

from backend.routes.tasks import roadmap_pdf_sections


def test_roadmap_pdf_sections_keep_complete_periods_and_paginate_only_scheduled_tasks():
    q1 = SimpleNamespace(id="q1", name="Q1", start_month=1, end_month=3)
    q2 = SimpleNamespace(id="q2", name="Q2", start_month=4, end_month=6)
    tasks = [SimpleNamespace(
        roadmap_period_id="q1", start_date=date(2027, 1, 4), due_date=date(2027, 1, 8),
        scheduled_week=None, scheduled_month=1,
    ) for _ in range(28)]
    tasks.append(SimpleNamespace(roadmap_period_id="q2", start_date=None, due_date=None, scheduled_week=None, scheduled_month=None))

    sections = roadmap_pdf_sections([q1, q2], tasks, 2027)
    assert [(section[0].name, len(section[1]), len(section[2]), section[3], section[4]) for section in sections] == [
        ("Q1", 13, 27, 1, 2),
        ("Q1", 13, 1, 2, 2),
    ]
    sections_with_empty = roadmap_pdf_sections([q1, q2], tasks, 2027, include_empty=True)
    assert [(section[0].name, len(section[2])) for section in sections_with_empty] == [("Q1", 27), ("Q1", 1), ("Q2", 0)]


def test_task_schedule_rejects_conflicting_year_quarter_month_and_week(client, project):
    q2 = client.get(f"/api/projects/{project['id']}/task-manager/roadmap?year=2027").json()["periods"][1]
    base = {"title": "Plan Q2 work", "roadmap_period_id": q2["id"], "scheduled_year": 2027}

    wrong_year = client.post(f"/api/projects/{project['id']}/task-manager/tasks", json={**base, "scheduled_year": 2028, "scheduled_month": 4})
    assert wrong_year.status_code == 422
    assert "year" in wrong_year.json()["detail"].lower()
    wrong_month = client.post(f"/api/projects/{project['id']}/task-manager/tasks", json={**base, "scheduled_month": 2})
    assert wrong_month.status_code == 422
    assert "quarter" in wrong_month.json()["detail"].lower()
    wrong_week = client.post(f"/api/projects/{project['id']}/task-manager/tasks", json={**base, "scheduled_month": 4, "scheduled_week": "2027-05-03"})
    assert wrong_week.status_code == 422
    assert "month" in wrong_week.json()["detail"].lower()

    valid = client.post(f"/api/projects/{project['id']}/task-manager/tasks", json={**base, "scheduled_month": 4, "scheduled_week": "2027-04-05"})
    assert valid.status_code == 201, valid.text


def test_task_crud_roadmap_history_tracker_and_exports(client, project):
    roadmap = client.get(f"/api/projects/{project['id']}/task-manager/roadmap?year=2027")
    assert roadmap.status_code == 200, roadmap.text
    body = roadmap.json()
    assert [item["name"] for item in body["periods"]] == ["Q1", "Q2", "Q3", "Q4"]
    assert body["periods"][0]["months"][0]["weeks"] == ["2027-01-04", "2027-01-11", "2027-01-18", "2027-01-25"]
    q1 = body["periods"][0]

    created = client.post(f"/api/projects/{project['id']}/task-manager/tasks", json={
        "title": "Repair missing title", "description": "Write and deploy a useful title.", "category": "On-Page SEO",
        "priority": "high", "status": "to_do", "roadmap_period_id": q1["id"], "scheduled_year": 2027,
        "scheduled_month": 1, "scheduled_week": "2027-01-04", "due_date": "2027-01-08",
        "related_urls": ["https://shop.example.com/page"], "supporting_links": [{"label": "Brief", "url": "https://example.com/brief"}],
        "source_issue_key": "on-page:missing-title:page", "source_issue_type": "missing_title", "source_section": "on_page",
    })
    assert created.status_code == 201, created.text
    task = created.json()

    duplicate_issue = client.post(f"/api/projects/{project['id']}/task-manager/tasks", json={
        "title": "Same issue again", "priority": "high", "status": "to_do", "source_issue_key": "on-page:missing-title:page"
    })
    assert duplicate_issue.status_code == 201
    assert duplicate_issue.json()["duplicate"] is True
    assert duplicate_issue.json()["id"] == task["id"]

    completed = client.patch(f"/api/projects/{project['id']}/task-manager/tasks/{task['id']}", json={"status": "completed", "completion_notes": "Released"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["completion_date"]
    assert completed.json()["verified_resolution"] is False

    detail = client.get(f"/api/projects/{project['id']}/task-manager/tasks/{task['id']}").json()
    assert [item["event_type"] for item in detail["history"]] == ["updated", "created"]
    assert len(detail["tracker_activity_ids"]) == 2
    tracker = client.get(f"/api/projects/{project['id']}/activity").json()["items"]
    assert all(item["task_id"] == task["id"] for item in tracker)
    assert {item["status"] for item in tracker} == {"in_progress", "completed"}
    assert {item["section"] for item in tracker} == {"task_manager"}
    assert client.get(f"/api/projects/{project['id']}/activity?section=task_manager").json()["total"] == 2
    assert client.get(f"/api/projects/{project['id']}/activity?section=pages").json()["total"] == 0

    assert client.get(f"/api/projects/{project['id']}/task-manager/export?format=csv").text.startswith("Title,")
    xlsx = client.get(f"/api/projects/{project['id']}/task-manager/export?format=xlsx")
    assert xlsx.status_code == 200
    assert xlsx.content[:2] == b"PK"
    roadmap_csv = client.get(f"/api/projects/{project['id']}/task-manager/roadmap/export?format=csv&year=2027&period_ids={q1['id']}&statuses=completed&priorities=high")
    assert roadmap_csv.status_code == 200, roadmap_csv.text
    assert "Task,Assigned Owner,Status,Priority,Start Date,Due Date,Quarter / Period,Month,Scheduled Week" in roadmap_csv.text
    assert "Repair missing title" in roadmap_csv.text
    roadmap_pdf = client.get(f"/api/projects/{project['id']}/task-manager/roadmap/export?format=pdf&year=2027&period_ids={q1['id']}&statuses=completed&priorities=high")
    assert roadmap_pdf.status_code == 200, roadmap_pdf.text
    assert roadmap_pdf.headers["content-type"] == "application/pdf"
    assert roadmap_pdf.content.startswith(b"%PDF-")

    excluded = client.get(f"/api/projects/{project['id']}/task-manager/roadmap/export?format=csv&year=2027&period_ids={q1['id']}&statuses=to_do")
    assert "Repair missing title" not in excluded.text


def test_period_deletion_requires_explicit_task_handling_and_bundle_preserves_tasks(client, project):
    roadmap = client.get(f"/api/projects/{project['id']}/task-manager/roadmap?year=2028").json()
    q1, q2 = roadmap["periods"][:2]
    task = client.post(f"/api/projects/{project['id']}/task-manager/tasks", json={
        "title": "Quarter task", "roadmap_period_id": q1["id"], "scheduled_year": 2028, "scheduled_month": 2,
    }).json()

    blocked = client.delete(f"/api/projects/{project['id']}/task-manager/periods/{q1['id']}")
    assert blocked.status_code == 409
    reassigned = client.delete(f"/api/projects/{project['id']}/task-manager/periods/{q1['id']}?reassign_to={q2['id']}")
    assert reassigned.status_code == 200, reassigned.text
    assert client.get(f"/api/projects/{project['id']}/task-manager/tasks/{task['id']}").json()["roadmap_period_id"] == q2["id"]

    exported = client.get(f"/api/projects/{project['id']}/export")
    restored = client.post("/api/projects/import", files={"file": ("tasks.seo-project.json", exported.content, "application/json")})
    assert restored.status_code == 201, restored.text
    restored_tasks = client.get(f"/api/projects/{restored.json()['id']}/task-manager/tasks").json()["items"]
    assert len(restored_tasks) == 1
    assert restored_tasks[0]["title"] == "Quarter task"
    assert restored_tasks[0]["roadmap_period_id"]


def test_linked_task_is_verified_only_after_later_crawl_removes_issue(client, project):
    def crawl(name, content, reporting_date):
        staged = client.post(f"/api/projects/{project['id']}/imports/stage", data={"source": "screaming_frog", "reporting_date": reporting_date}, files={"file": (name, content, "text/csv")})
        assert staged.status_code == 201, staged.text
        data = staged.json()
        confirmed = client.post(f"/api/projects/{project['id']}/imports/{data['batch']['id']}/confirm", json={"mapping": data["suggested_mapping"], "headers_verified": data["requires_header_verification"]})
        assert confirmed.status_code == 200, confirmed.text

    crawl("before.csv", b"Address,Status Code,Content Type,Title 1\nhttps://shop.example.com/page,200,text/html,\n", "2027-01-10")
    issue = next(item for item in client.get(f"/api/projects/{project['id']}/issues").json() if item["type"] == "missing_title")
    task = client.post(f"/api/projects/{project['id']}/task-manager/tasks", json={
        "title": "Add the missing title", "status": "in_progress", "source_issue_key": issue["issue_key"],
        "source_issue_type": issue["type"], "source_section": issue["source"], "source_url": issue["url"], "related_urls": [issue["url"]],
    }).json()
    assert task["verified_resolution"] is False

    crawl("after.csv", b"Address,Status Code,Content Type,Title 1\nhttps://shop.example.com/page,200,text/html,A useful title that is comfortably within guidance\n", "2027-02-10")
    verified = client.get(f"/api/projects/{project['id']}/task-manager/tasks/{task['id']}").json()
    assert verified["verified_resolution"] is True
    assert verified["status"] == "in_progress"
    assert verified["verification_data"]["file_name"] == "after.csv"
    assert verified["history"][0]["event_type"] == "verified_resolved"
    tracker = client.get(f"/api/projects/{project['id']}/activity?status=resolved").json()["items"]
    assert any(item["task_id"] == task["id"] and item["action_type"] == "task_verified_resolved" for item in tracker)
    assert next(item for item in tracker if item["task_id"] == task["id"] and item["action_type"] == "task_verified_resolved")["section"] == "task_manager"
