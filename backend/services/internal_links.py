from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditCase, ImportBatch, ImportRow, InternalLinkObservation, InternalLinkSnapshot, Keyword, Page, PageRelationship, Project, SitemapEntry, TargetMapping
from .activity import add_activity
from .issue_priorities import resolve_priority
from .on_page import latest_metadata
from .urls import normalize_url

CATEGORIES = {
    "broken_invalid_links": "Broken & Invalid Links",
    "orphan_weak_pages": "Orphan & Weakly Linked Pages",
    "architecture_crawl_depth": "Architecture & Crawl Depth",
    "anchor_text": "Anchor Text Issues",
    "indexability_canonical_conflicts": "Indexability & Canonical Conflicts",
    "linking_opportunities": "Internal Linking Opportunities",
    "distribution_structural_patterns": "Link Distribution & Structural Patterns",
}
GENERIC_ANCHORS = {"click here", "here", "read more", "learn more", "more", "view", "details"}


def _int(value):
    try:
        return int(float(str(value).strip())) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def page_crawl_link_metrics(page: Page) -> dict[str, int | None]:
    """Return mapped page-level Screaming Frog metrics without inferring them."""
    recognized = ((page.crawl_data or {}).get("recognized") or {}) if page else {}
    return {
        "crawl_depth": _int(recognized.get("crawl_depth")),
        "imported_inlinks": _int(recognized.get("inlinks")),
    }


def effective_internal_inlinks(page: Page, observed_count: int | None) -> tuple[int | None, str | None]:
    """Prefer Screaming Frog's page metric, with relationship evidence as fallback."""
    imported = page_crawl_link_metrics(page)["imported_inlinks"]
    if imported is not None:
        return imported, "screaming_frog"
    if observed_count is not None:
        return observed_count, "calculated_relationships"
    return None, None


def _project_host(project: Project) -> str:
    return (urlsplit(project.site_url).hostname or "").casefold()


def _normalize(value: str, project: Project) -> str | None:
    try:
        return normalize_url(value, project.preferences)
    except (TypeError, ValueError):
        return None


def confirm_internal_link_import(db: Session, batch: ImportBatch, project: Project, mapping: dict[str, str], headers_verified: bool, complete_snapshot: bool) -> ImportBatch:
    if any(str(w).startswith("Header verification required:") for w in batch.warnings) and not headers_verified:
        raise HTTPException(422, "Review and verify non-exact Inlinks header matches before importing")
    if not mapping.get("source_url") or not mapping.get("destination_url"):
        raise HTTPException(422, "Map both Source URL and Destination URL before importing")
    previous_batch = db.scalar(select(ImportBatch).where(
        ImportBatch.project_id == project.id, ImportBatch.source == "screaming_frog_inlinks",
        ImportBatch.checksum == batch.checksum, ImportBatch.status.like("completed%"), ImportBatch.id != batch.id,
    ).order_by(ImportBatch.imported_at.desc()))
    rows = list(db.scalars(select(ImportRow).where(ImportRow.batch_id == batch.id).order_by(ImportRow.row_number)))
    if previous_batch:
        for row in rows:
            row.validation_status = "duplicate_import"
        batch.mapping = mapping
        batch.skipped_count = len(rows)
        batch.status = "completed"
        batch.warnings = [*batch.warnings, f"No link observations were duplicated; this file matches completed import {previous_batch.file_name}."]
        db.commit()
        return batch

    previous_snapshot = latest_snapshot(db, project.id)
    previous_findings = {item["issue_key"]: item for item in internal_link_findings(db, project)} if previous_snapshot else {}
    snapshot = InternalLinkSnapshot(project_id=project.id, import_batch_id=batch.id, complete_snapshot=complete_snapshot, observed_at=batch.imported_at)
    db.add(snapshot)
    db.flush()
    pages = {page.normalized_url: page for page in db.scalars(select(Page).where(Page.project_id == project.id))}
    host = _project_host(project)
    imported = external = unmatched = errors = 0
    for row in rows:
        source = str(row.raw_data.get(mapping["source_url"]) or "").strip()
        destination = str(row.raw_data.get(mapping["destination_url"]) or "").strip()
        source_normalized, destination_normalized = _normalize(source, project), _normalize(destination, project)
        if not source or not destination or not source_normalized or not destination_normalized:
            row.validation_status = "error"
            row.error_message = "Source and Destination must be valid absolute URLs"
            errors += 1
            continue
        internal = (urlsplit(source_normalized).hostname or "").casefold() == host and (urlsplit(destination_normalized).hostname or "").casefold() == host
        source_page = pages.get(source_normalized)
        destination_page = pages.get(destination_normalized)
        values = {key: row.raw_data.get(column) for key, column in mapping.items() if column}
        observation = InternalLinkObservation(
            project_id=project.id, snapshot_id=snapshot.id, import_batch_id=batch.id, row_number=row.row_number,
            source_page_id=source_page.id if source_page else None, destination_page_id=destination_page.id if destination_page else None,
            source_url=source, normalized_source_url=source_normalized, destination_url=destination, normalized_destination_url=destination_normalized,
            is_internal=internal, link_type=values.get("link_type") or None, size_bytes=_int(values.get("size_bytes")), alt_text=values.get("alt_text") or None,
            anchor=values.get("anchor") or None, status_code=_int(values.get("status_code")), status_text=values.get("status_text") or None,
            follow=values.get("follow") or None, target=values.get("target") or None, rel=values.get("rel") or None, path_type=values.get("path_type") or None,
            link_path=values.get("link_path") or None, link_position=values.get("link_position") or None, link_origin=values.get("link_origin") or None,
        )
        db.add(observation)
        row.normalized_data = {"source_url": source_normalized, "destination_url": destination_normalized, "is_internal": internal, "recognized_fields": values}
        row.page_id = destination_page.id if destination_page else None
        row.validation_status = "imported" if internal else "external_excluded"
        imported += int(internal)
        external += int(not internal)
        unmatched += int(internal and (not source_page or not destination_page))
    snapshot.internal_count, snapshot.external_count, snapshot.unmatched_count = imported, external, unmatched
    batch.mapping = mapping
    batch.imported_count, batch.skipped_count, batch.error_count = imported + external, external, errors
    batch.status = "completed_with_errors" if errors else "completed"
    if errors:
        batch.warnings = [*batch.warnings, f"{errors} row(s) had invalid Source or Destination URLs."]
    db.flush()
    current_findings = {item["issue_key"]: item for item in internal_link_findings(db, project)}
    if previous_snapshot:
        before_counts, after_counts = snapshot_page_counts(db, previous_snapshot.id), snapshot_page_counts(db, snapshot.id)
        pages_by_id = {page.id: page for page in db.scalars(select(Page).where(Page.project_id == project.id, Page.id.in_(set(before_counts) | set(after_counts))))}
        for page_id in sorted(set(before_counts) | set(after_counts)):
            before = before_counts.get(page_id, {"inlinks": 0, "unique_sources": 0})
            after = after_counts.get(page_id, {"inlinks": 0, "unique_sources": 0})
            if before == after:
                continue
            page = pages_by_id.get(page_id)
            add_activity(db, project.id, event_key=f"internal-links:counts:{batch.id}:{page_id}", source="site_architecture", section="site_architecture", action_type="internal_link_coverage_changed", status="changed", title="Internal-link coverage changed", notes="Counts are directly comparable only when both snapshots are complete." if not (previous_snapshot.complete_snapshot and complete_snapshot) else "Compared two complete Inlinks snapshots.", page_id=page_id, url=page.original_url if page else None, before_data={**before, "snapshot_id": previous_snapshot.id, "complete": previous_snapshot.complete_snapshot}, after_data={**after, "snapshot_id": snapshot.id, "complete": complete_snapshot})
    for issue_key in sorted(current_findings.keys() - previous_findings.keys()):
        finding = current_findings[issue_key]
        add_activity(db, project.id, event_key=f"internal-links:new:{batch.id}:{issue_key}", source="site_architecture", section="site_architecture", action_type="internal_link_issue_detected", status="in_progress", title=finding["label"], notes=finding["detail"], page_id=finding.get("page_id"), url=finding.get("url"), issue_key=issue_key, issue_type=finding["type"], after_data={"snapshot_id": snapshot.id, "evidence": finding.get("evidence"), "recommendation": finding.get("recommendation")})
    # Absence is resolution evidence only when both comparable exports were
    # explicitly confirmed as complete. Partial files never close findings.
    if previous_snapshot and previous_snapshot.complete_snapshot and complete_snapshot:
        for issue_key in sorted(previous_findings.keys() - current_findings.keys()):
            finding = previous_findings[issue_key]
            add_activity(db, project.id, event_key=f"internal-links:resolved:{batch.id}:{issue_key}", source="site_architecture", section="site_architecture", action_type="internal_link_issue_resolved", status="resolved", title=finding["label"], notes="The finding is absent from the latest complete Inlinks snapshot. This records observed resolution, not attribution to a recommendation.", page_id=finding.get("page_id"), url=finding.get("url"), issue_key=issue_key, issue_type=finding["type"], before_data={"snapshot_id": previous_snapshot.id, "evidence": finding.get("evidence")}, after_data={"snapshot_id": snapshot.id, "present": False})
    add_activity(db, project.id, event_key=f"internal-links:{batch.checksum}", source="site_architecture", section="site_architecture", action_type="internal_links_imported", status="changed", title=f"Imported internal-link snapshot: {batch.file_name}", notes="External destinations are retained as evidence but excluded from internal-link calculations.", after_data={"snapshot_id": snapshot.id, "internal_links": imported, "external_links": external, "unmatched_links": unmatched, "complete_snapshot": complete_snapshot})
    db.commit()
    return batch


def latest_snapshot(db: Session, project_id: str) -> InternalLinkSnapshot | None:
    return db.scalar(select(InternalLinkSnapshot).where(InternalLinkSnapshot.project_id == project_id).order_by(InternalLinkSnapshot.observed_at.desc(), InternalLinkSnapshot.id.desc()))


def page_link_counts(db: Session, project_id: str) -> dict[str, dict[str, int]]:
    snapshot = latest_snapshot(db, project_id)
    if not snapshot:
        return {}
    rows = list(db.scalars(select(InternalLinkObservation).where(InternalLinkObservation.snapshot_id == snapshot.id, InternalLinkObservation.is_internal.is_(True))))
    occurrences, sources = Counter(), defaultdict(set)
    for row in rows:
        if row.destination_page_id:
            occurrences[row.destination_page_id] += 1
            sources[row.destination_page_id].add(row.normalized_source_url)
    return {page_id: {"inlinks": count, "unique_sources": len(sources[page_id])} for page_id, count in occurrences.items()}


def snapshot_page_counts(db: Session, snapshot_id: str) -> dict[str, dict[str, int]]:
    rows = list(db.scalars(select(InternalLinkObservation).where(InternalLinkObservation.snapshot_id == snapshot_id, InternalLinkObservation.is_internal.is_(True))))
    occurrences, sources = Counter(), defaultdict(set)
    for row in rows:
        if row.destination_page_id:
            occurrences[row.destination_page_id] += 1
            sources[row.destination_page_id].add(row.normalized_source_url)
    return {page_id: {"inlinks": count, "unique_sources": len(sources[page_id])} for page_id, count in occurrences.items()}


def internal_link_findings(db: Session, project: Project) -> list[dict]:
    snapshot = latest_snapshot(db, project.id)
    if not snapshot:
        return []
    observations = list(db.scalars(select(InternalLinkObservation).where(InternalLinkObservation.snapshot_id == snapshot.id, InternalLinkObservation.is_internal.is_(True))))
    pages = list(db.scalars(select(Page).where(Page.project_id == project.id, Page.page_type.not_in(["Draft", "Asset"]))))
    by_id = {p.id: p for p in pages}
    incoming, unique_sources, outgoing = Counter(), defaultdict(set), Counter()
    anchors_by_destination = defaultdict(set)
    metadata = latest_metadata(db, project.id)
    keywords = {item.id: item.text for item in db.scalars(select(Keyword).where(Keyword.project_id == project.id))}
    mapped_keywords: dict[str, list[str]] = defaultdict(list)
    for mapping in db.scalars(select(TargetMapping).where(TargetMapping.project_id == project.id, TargetMapping.page_id.is_not(None))):
        if mapping.keyword_id and keywords.get(mapping.keyword_id):
            mapped_keywords[mapping.page_id].append(keywords[mapping.keyword_id])
    approved_parents = {item.target_page_id: item.source_page_id for item in db.scalars(select(PageRelationship).where(PageRelationship.project_id == project.id, PageRelationship.relationship_type == "primary_parent", PageRelationship.status == "approved"))}
    findings: list[dict] = []

    def add(issue_type, category, label, detail, page=None, observation=None, severity="warning", recommendation="Review and update the affected internal links.", evidence=None, identity_override=None, url_override=None):
        context_page = page or (by_id.get(observation.source_page_id) if observation else None)
        target_page = by_id.get(observation.destination_page_id) if observation else page
        page_metrics = page_crawl_link_metrics(context_page) if context_page else {"crawl_depth": None, "imported_inlinks": None}
        crawl_depth = page_metrics["crawl_depth"]
        imported_inlinks = page_metrics["imported_inlinks"]
        observed_inlinks = incoming.get(context_page.id, 0) if context_page else None
        observed_sources = len(unique_sources.get(context_page.id, set())) if context_page else None
        internal_inlinks, internal_inlinks_source = effective_internal_inlinks(context_page, observed_inlinks) if context_page else (None, None)
        status_code = observation.status_code if observation else target_page.http_status if target_page else None
        url = url_override or (page.original_url if page else observation.source_url if observation else None)
        identity = identity_override or (page.id if page else f"{observation.normalized_source_url}>{observation.normalized_destination_url}:{observation.row_number}" if observation else "project")
        priority = resolve_priority(project.preferences, "site_architecture", issue_type, "high" if severity == "error" else "medium" if severity == "warning" else "low")
        findings.append({"issue_key": f"architecture:internal_link:{issue_type}:{identity}", "type": issue_type, "category": CATEGORIES[category], "category_key": category,
            "label": label, "detail": detail, "severity": severity, "priority": priority, "page_id": page.id if page else observation.source_page_id if observation else None,
            "url": url, "page_type": page.page_type if page else by_id.get(observation.source_page_id).page_type if observation and observation.source_page_id in by_id else None,
            "inlinks": observed_inlinks, "imported_inlinks": imported_inlinks, "internal_inlinks": internal_inlinks,
            "internal_inlinks_source": internal_inlinks_source, "unique_sources": observed_sources,
            "crawl_depth": crawl_depth, "indexability": context_page.indexability if context_page else None, "status_code": status_code,
            "page_status_code": context_page.http_status if context_page else None,
            "recommendation": recommendation, "snapshot_id": snapshot.id, "snapshot_complete": snapshot.complete_snapshot, "evidence": evidence or ({"source_url": observation.source_url, "destination_url": observation.destination_url, "anchor": observation.anchor, "status_code": observation.status_code, "follow": observation.follow, "rel": observation.rel, "link_position": observation.link_position, "link_origin": observation.link_origin} if observation else {})})

    batch = db.get(ImportBatch, snapshot.import_batch_id)
    mapping = (batch.mapping or {}) if batch else {}
    for row in db.scalars(select(ImportRow).where(ImportRow.batch_id == snapshot.import_batch_id, ImportRow.validation_status == "error")):
        source = row.raw_data.get(mapping.get("source_url", ""))
        destination = row.raw_data.get(mapping.get("destination_url", ""))
        add("invalid_internal_destination", "broken_invalid_links", "Invalid internal-link record", row.error_message or "Source or destination URL could not be validated.", severity="warning", recommendation="Correct or remove the malformed source/destination URL in the originating page or export.", evidence={"source_url": source, "destination_url": destination, "row_number": row.row_number}, identity_override=f"invalid:{snapshot.id}:{row.row_number}", url_override=source)

    # Build coverage metrics before emitting findings so every row uses the
    # complete snapshot rather than only the observations processed so far.
    for row in observations:
        if row.destination_page_id:
            incoming[row.destination_page_id] += 1
            unique_sources[row.destination_page_id].add(row.normalized_source_url)
            anchor = (row.anchor or row.alt_text or "").strip()
            if anchor:
                anchors_by_destination[row.destination_page_id].add(anchor.casefold())
        if row.source_page_id:
            outgoing[row.source_page_id] += 1

    for row in observations:
        anchor = (row.anchor or row.alt_text or "").strip()
        status = row.status_code or (by_id.get(row.destination_page_id).http_status if row.destination_page_id in by_id else None)
        if status and status >= 400:
            add("broken_internal_link", "broken_invalid_links", "Broken internal link", f"{row.source_url} links to {row.destination_url}, which returned {status}.", observation=row, severity="error", recommendation="Replace or remove the link, or restore the destination.")
        elif status and 300 <= status < 400:
            add("redirecting_internal_link", "broken_invalid_links", "Internal link passes through a redirect", f"Destination returned {status}.", observation=row, recommendation="Update the link to its final canonical destination where appropriate.")
        if not anchor:
            add("empty_anchor", "anchor_text", "Empty internal-link anchor", "No anchor or image alt text was supplied for this link.", observation=row, recommendation="Add concise, descriptive anchor or image alt text.")
        elif anchor.casefold() in GENERIC_ANCHORS:
            add("generic_anchor", "anchor_text", "Generic anchor text", f'Anchor text is “{anchor}”.', observation=row, severity="info", recommendation="Use descriptive text that explains the destination when natural.")
        destination = by_id.get(row.destination_page_id)
        if destination and destination.indexability and "non" in destination.indexability.casefold():
            add("link_to_non_indexable", "indexability_canonical_conflicts", "Internal link targets a non-indexable page", destination.indexability_status or destination.indexability, observation=row, severity="error", recommendation="Confirm intent; otherwise link to an indexable canonical page.")
        canonical = str(((metadata.get(row.destination_page_id) or {}).get("current") or {}).get("canonical_url") or "").strip()
        canonical_normalized = _normalize(canonical, project) if canonical else None
        if destination and canonical_normalized and canonical_normalized != destination.normalized_url:
            add("link_to_non_canonical", "indexability_canonical_conflicts", "Internal link targets a non-canonical URL", f"The destination canonicalises to {canonical}.", observation=row, severity="warning", recommendation="Link directly to the canonical target when the canonical is intentional.", evidence={"source_url": row.source_url, "destination_url": row.destination_url, "canonical_url": canonical, "anchor": row.anchor})

    sitemap_ids = set(db.scalars(select(SitemapEntry.page_id).where(SitemapEntry.project_id == project.id, SitemapEntry.page_id.is_not(None))))
    weak_threshold = int(((project.preferences or {}).get("internal_linking") or {}).get("weak_inlinks", 3))
    depth_threshold = int(((project.preferences or {}).get("internal_linking") or {}).get("deep_crawl_depth", 4))
    average_inlinks = sum(incoming.values()) / max(len(pages), 1)
    high_inlink_threshold = max(20, int(average_inlinks * 5))
    important = {"Homepage", "PLP", "PDP", "Landing", "Brand"}
    for page in pages:
        count = incoming.get(page.id, 0)
        sources = len(unique_sources.get(page.id, set()))
        crawl_depth = _int(((page.crawl_data or {}).get("recognized") or {}).get("crawl_depth"))
        if count == 0:
            add("potential_orphan_page", "orphan_weak_pages", "Potential orphan page", "No internal inlinks were observed in the latest supplied snapshot. This is not definitive unless the export is complete.", page=page, severity="warning" if page.page_type in important else "info", recommendation="Verify crawl completeness, then add relevant contextual or navigational links.", evidence={"complete_snapshot": snapshot.complete_snapshot})
        elif sources < weak_threshold:
            add("weakly_linked_page", "orphan_weak_pages", "Weakly linked page", f"Observed {count} inlinks from {sources} unique source page(s).", page=page, severity="warning" if page.page_type in important else "info", recommendation="Add links from relevant, authoritative pages where useful.")
        if page.id in sitemap_ids and count == 0:
            add("sitemap_potential_orphan", "orphan_weak_pages", "Sitemap page has no observed inlinks", "The URL is sitemap-listed but has no observed internal inlinks.", page=page, severity="warning")
        if crawl_depth is not None and crawl_depth >= depth_threshold and page.page_type in important:
            add("deep_important_page", "architecture_crawl_depth", "Important page has high crawl depth", f"Latest supplied crawl depth is {crawl_depth}.", page=page, recommendation="Review hierarchy and add relevant links from shallower pages.", evidence={"crawl_depth": crawl_depth})
        if page.page_type in important and sources < weak_threshold:
            parent = by_id.get(approved_parents.get(page.id))
            page_keywords = mapped_keywords.get(page.id, [])
            add("internal_link_opportunity", "linking_opportunities", "Internal linking opportunity", f"This {page.page_type} has only {sources} unique observed source page(s).", page=page, severity="info", recommendation="Review the suggested source and anchor in context before implementing. This is a proposal, not an observed link.", evidence={"target_url": page.original_url, "suggested_source_urls": [parent.original_url] if parent else [], "suggested_anchor_text": page_keywords[0] if page_keywords else None, "mapped_keywords": page_keywords, "reason": "Approved parent relationship and/or mapped keyword evidence" if parent or page_keywords else "Important weakly linked page; source-page review required", "observed_link": False})
        if len(anchors_by_destination.get(page.id, set())) >= 6:
            add("inconsistent_anchor_text", "anchor_text", "Highly varied anchor text", f"Observed {len(anchors_by_destination[page.id])} distinct anchors for this destination.", page=page, severity="info", recommendation="Review whether anchor variation is intentional and aligned with page purpose.")
        if count >= high_inlink_threshold:
            add("unusually_high_inlinks", "distribution_structural_patterns", "Unusually high internal-link volume", f"Observed {count} link occurrences from {sources} unique source pages; the project average is {average_inlinks:.1f}.", page=page, severity="info", recommendation="Review template repetition and whether the distribution is intentional; high link volume is not automatically a problem.", evidence={"inlinks": count, "unique_sources": sources, "project_average_inlinks": round(average_inlinks, 2), "threshold": high_inlink_threshold})

    parents = list(db.scalars(select(PageRelationship).where(PageRelationship.project_id == project.id, PageRelationship.relationship_type == "primary_parent", PageRelationship.status == "approved")))
    observed_pairs = {(row.source_page_id, row.destination_page_id) for row in observations}
    for relation in parents:
        if (relation.source_page_id, relation.target_page_id) not in observed_pairs and relation.target_page_id in by_id:
            child, parent = by_id[relation.target_page_id], by_id.get(relation.source_page_id)
            add("missing_parent_child_link", "architecture_crawl_depth", "Approved parent does not link to child", "The manually approved hierarchy relationship was not observed in the Inlinks snapshot.", page=child, severity="info", recommendation="Verify the snapshot, then consider adding a navigational or contextual parent-to-child link.", evidence={"parent_url": parent.original_url if parent else None, "relationship_id": relation.id})
    position_counts = Counter((row.link_position or row.link_origin or "Unknown") for row in observations)
    total = max(len(observations), 1)
    for position, count in position_counts.items():
        if count / total >= .75 and count >= 10:
            add("template_link_concentration", "distribution_structural_patterns", "Links are concentrated in one structural position", f"{count} of {total} observed internal links are in {position}.", severity="info", recommendation="Review whether contextual body links are underused; template links are not automatically a problem.", evidence={"position": position, "count": count, "total": total})
    priority_overrides = {case.issue_key: case.priority_override for case in db.scalars(select(AuditCase).where(AuditCase.project_id == project.id, AuditCase.issue_key.is_not(None), AuditCase.priority_override.is_not(None)))}
    for finding in findings:
        if priority_overrides.get(finding["issue_key"]):
            finding["priority"] = priority_overrides[finding["issue_key"]]
    return findings


def page_inlinks(db: Session, project_id: str, page_id: str) -> dict:
    snapshot = latest_snapshot(db, project_id)
    if not snapshot:
        return {"snapshot": None, "inlinks": [], "count": 0, "unique_sources": 0}
    rows = list(db.scalars(select(InternalLinkObservation).where(InternalLinkObservation.snapshot_id == snapshot.id, InternalLinkObservation.is_internal.is_(True), InternalLinkObservation.destination_page_id == page_id).order_by(InternalLinkObservation.source_url, InternalLinkObservation.row_number)))
    return {"snapshot": {"id": snapshot.id, "observed_at": snapshot.observed_at, "complete": snapshot.complete_snapshot}, "count": len(rows), "unique_sources": len({r.normalized_source_url for r in rows}), "inlinks": [{"id": r.id, "source_url": r.source_url, "destination_url": r.destination_url, "anchor": r.anchor, "alt_text": r.alt_text, "status_code": r.status_code, "follow": r.follow, "rel": r.rel, "link_position": r.link_position, "link_origin": r.link_origin} for r in rows]}
