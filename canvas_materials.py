#!/usr/bin/env python3
"""Download Canvas files linked from assignment descriptions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from canvas_periods import assignment_matches_period, parse_period_selector, select_grading_periods
from canvas_scraper import login_if_needed

FILE_ID_PATTERN = re.compile(r"/files/(\d+)(?:/|$)")


class AssignmentFileLinkParser(HTMLParser):
    """Collect file URLs from links and embedded media in assignment HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        keys = ("href", "src", "data-api-endpoint") if tag in ("a", "img", "iframe", "source") else ("data-api-endpoint",)
        for key in keys:
            value = attributes.get(key)
            if value and "/files/" in value:
                self.urls.append(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/canvas.sqlite3"), help="SQLite database path")
    parser.add_argument("--student", help="Limit to a student whose name contains this text")
    parser.add_argument("--course", help="Limit to a course whose name contains this text")
    parser.add_argument("--all-periods", action="store_true", help="Include assignments from all stored periods")
    periods = parser.add_mutually_exclusive_group()
    periods.add_argument("--quarter", metavar="YYYY-QN", help="Select a school-year quarter, e.g. 2020-Q1")
    periods.add_argument("--semester", metavar="YYYY-SN", help="Select two quarters, e.g. 2020-S1")
    parser.add_argument("--dry-run", action="store_true", help="List file references without downloading")
    parser.add_argument("--refresh", action="store_true", help="Re-download files already present locally")
    parser.add_argument("--limit", type=int, default=500, help="Maximum unique files to process (default: 500)")
    parser.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds (default: 60)")
    parser.add_argument("--max-mb", type=int, default=250, help="Maximum file size in MB (default: 250)")
    parser.add_argument("--headed", action="store_true", help="Show the browser for interactive sign-in/MFA")
    return parser.parse_args()


def canvas_file_ids(assignment: dict, origin: str) -> set[int]:
    parser = AssignmentFileLinkParser()
    parser.feed(assignment.get("description") or "")
    origin_host = urlparse(origin).netloc.casefold()
    found: set[int] = set()
    for raw_url in parser.urls:
        absolute = urljoin(origin.rstrip("/") + "/", raw_url)
        parsed = urlparse(absolute)
        if parsed.scheme != "https" or parsed.netloc.casefold() != origin_host:
            continue
        match = FILE_ID_PATTERN.search(parsed.path)
        if match:
            found.add(int(match.group(1)))

    for attachment in assignment.get("attachments", []) or []:
        if not isinstance(attachment, dict):
            continue
        attachment_url = attachment.get("url") or attachment.get("href") or ""
        parsed = urlparse(urljoin(origin.rstrip("/") + "/", attachment_url))
        match = FILE_ID_PATTERN.search(parsed.path)
        if parsed.scheme == "https" and parsed.netloc.casefold() == origin_host and match:
            found.add(int(match.group(1)))
        elif isinstance(attachment.get("id"), int) and attachment.get("content-type"):
            found.add(attachment["id"])
    return found


def find_assignment_files(db_path: Path, origin: str, student_filter: str | None,
                          course_filter: str | None, all_periods: bool, period_selector: dict) -> dict[int, list[dict]]:
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            WITH latest AS (
                SELECT student_id, course_id, MAX(scrape_run_id) AS run_id
                FROM course_scrapes GROUP BY student_id, course_id
            )
            SELECT s.name AS student, cs.course_id, COALESCE(c.name, 'Course ' || cs.course_id) AS course,
                   cs.scrape_run_id, a.assignment_id, a.name AS assignment, a.raw_json AS assignment_json,
                   cs.raw_course_json AS course_json, e.raw_json AS enrollment_json,
            (SELECT json_group_array(json_object('id', p.grading_period_id, 'title', p.title,
                              'start_at', p.start_at, 'end_at', p.end_at))
                    FROM grading_periods AS p
                    WHERE p.scrape_run_id=cs.scrape_run_id AND p.student_id=cs.student_id
                      AND p.course_id=cs.course_id) AS periods_json,
                   sub.raw_json AS submission_json
            FROM latest l
            JOIN course_scrapes cs
              ON cs.student_id=l.student_id AND cs.course_id=l.course_id AND cs.scrape_run_id=l.run_id
            JOIN students s ON s.id=cs.student_id
            LEFT JOIN courses c ON c.id=cs.course_id
            JOIN enrollments e
              ON e.scrape_run_id=cs.scrape_run_id AND e.student_id=cs.student_id AND e.course_id=cs.course_id
            JOIN assignments a
              ON a.scrape_run_id=cs.scrape_run_id AND a.student_id=cs.student_id AND a.course_id=cs.course_id
            LEFT JOIN submissions sub
              ON sub.scrape_run_id=a.scrape_run_id AND sub.student_id=a.student_id
             AND sub.course_id=a.course_id AND sub.assignment_id=a.assignment_id
            WHERE e.enrollment_state='active'
              AND COALESCE(json_extract(a.raw_json, '$.published'), 1) <> 0
              AND (:student IS NULL OR lower(s.name) LIKE '%' || lower(:student) || '%')
              AND (:course IS NULL OR lower(c.name) LIKE '%' || lower(:course) || '%')
            ORDER BY s.name, c.name, a.name
        """, {"student": student_filter, "course": course_filter}).fetchall()
    finally:
        connection.close()

    references: dict[int, list[dict]] = {}
    for row in rows:
        assignment = json.loads(row["assignment_json"] or "{}")
        submission = json.loads(row["submission_json"] or "{}")
        enrollment = json.loads(row["enrollment_json"] or "{}")
        course_data = json.loads(row["course_json"] or "{}")
        periods = select_grading_periods(
            json.loads(row["periods_json"] or "[]"), course_data,
            enrollment.get("current_grading_period_id"), period_selector,
        )
        if not all_periods and not assignment_matches_period(assignment, submission, periods):
            continue
        for file_id in canvas_file_ids(assignment, origin):
            references.setdefault(file_id, []).append({
                "student": row["student"],
                "course_id": row["course_id"],
                "course": row["course"],
                "assignment_id": row["assignment_id"],
                "assignment": row["assignment"],
            })
    return references


def safe_filename(name: str, file_id: int) -> str:
    name = Path(name or "file").name
    name = re.sub(r"[^A-Za-z0-9._() -]+", "_", name).strip(" .")
    if not name:
        name = "file"
    return f"{file_id}-{name[:160]}"


def download_files(references: dict[int, list[dict]], origin: str, output_root: Path,
                   timeout_ms: int, max_bytes: int, refresh: bool, headed: bool) -> int:
    load_dotenv()
    username = os.getenv("CANVAS_USERNAME", "")
    password = os.getenv("CANVAS_PASSWORD", "")
    login_url = os.getenv("CANVAS_URL", "")
    if not username or not password or not login_url:
        print("Set CANVAS_USERNAME, CANVAS_PASSWORD, and CANVAS_URL in .env.", file=sys.stderr)
        return 2

    output_root.mkdir(parents=True, exist_ok=True)
    os.chmod(output_root, 0o700)
    manifest_path = output_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        manifest = {}

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not headed)
            page = browser.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
            attempted_login = login_if_needed(page, username, password, timeout_ms, headed)
            if headed and attempted_login:
                input("Complete any sign-in or MFA prompts in Chromium, then press Enter here...")
            page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
            if page.locator('input#pseudonym_session_unique_id, input[name="pseudonym_session[unique_id]"]').count():
                raise RuntimeError("Canvas sign-in did not complete.")

            request = page.context.request
            downloaded = 0
            failed = 0
            for file_id, refs in sorted(references.items()):
                existing = manifest.get(str(file_id), {})
                local_name = existing.get("local_name")
                local_path = output_root / "files" / local_name if local_name else None
                if local_path and local_path.is_file() and not refresh:
                    existing["status"] = "already present"
                    existing["references"] = refs
                    manifest[str(file_id)] = existing
                    print(f"Already present: {local_path}")
                    continue

                try:
                    metadata_response = request.get(
                        f"{origin}/api/v1/files/{file_id}", timeout=timeout_ms
                    )
                    metadata = metadata_response.json() if metadata_response.ok else {}
                    filename = metadata.get("filename") or metadata.get("display_name") or f"file-{file_id}"
                    size = metadata.get("size")
                    if metadata.get("locked") or metadata.get("locked_for_user"):
                        raise RuntimeError("Canvas reports this file as locked.")
                    if isinstance(size, (int, float)) and size > max_bytes:
                        raise RuntimeError(f"File is larger than the configured {max_bytes // (1024 * 1024)} MB limit.")

                    download_url = metadata.get("url") or f"{origin}/files/{file_id}/download?download_frd=1"
                    download_url = urljoin(origin.rstrip("/") + "/", download_url)
                    if urlparse(download_url).scheme != "https":
                        raise RuntimeError("Canvas returned a non-HTTPS file URL.")
                    response = request.get(download_url, timeout=timeout_ms, max_redirects=10)
                    if not response.ok:
                        raise RuntimeError(f"Canvas file download returned HTTP {response.status}.")
                    content = response.body()
                    if len(content) > max_bytes:
                        raise RuntimeError(f"Downloaded file exceeds the configured {max_bytes // (1024 * 1024)} MB limit.")

                    local_name = safe_filename(filename, file_id)
                    files_dir = output_root / "files"
                    files_dir.mkdir(parents=True, exist_ok=True)
                    os.chmod(files_dir, 0o700)
                    destination = files_dir / local_name
                    destination.write_bytes(content)
                    os.chmod(destination, 0o600)
                    manifest[str(file_id)] = {
                        "file_id": file_id,
                        "filename": filename,
                        "local_name": local_name,
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "content_type": response.headers.get("content-type"),
                        "downloaded_at": datetime.now(timezone.utc).isoformat(),
                        "status": "downloaded",
                        "references": refs,
                    }
                    downloaded += 1
                    print(f"Downloaded: {filename} -> {destination}")
                except Exception as exc:
                    failed += 1
                    manifest[str(file_id)] = {
                        "file_id": file_id,
                        "status": "failed",
                        "error": str(exc),
                        "references": refs,
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                    print(f"Could not download Canvas file {file_id}: {exc}", file=sys.stderr)

            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.chmod(manifest_path, 0o600)
            browser.close()
    except Exception as exc:
        print(f"Canvas material download failed: {exc}", file=sys.stderr)
        return 1

    print(f"Downloaded {downloaded} file(s); {failed} failed. Saved under {output_root}.")
    return 1 if failed else 0


def main() -> int:
    load_dotenv()
    args = parse_args()
    try:
        period_selector = parse_period_selector(args.quarter, args.semester)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.all_periods and period_selector["academic_year"] is not None:
        print("Use either --all-periods or --quarter/--semester, not both.", file=sys.stderr)
        return 2
    if not 1 <= args.limit <= 5000:
        print("--limit must be between 1 and 5000.", file=sys.stderr)
        return 2
    if args.timeout <= 0 or args.max_mb <= 0:
        print("--timeout and --max-mb must be positive.", file=sys.stderr)
        return 2
    if not args.db.is_file():
        print(f"Database not found: {args.db}. Run `canvas sync` first.", file=sys.stderr)
        return 2

    canvas_url = os.getenv("CANVAS_URL", "")
    parsed_url = urlparse(canvas_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        print("Set CANVAS_URL to your HTTPS Canvas instance in .env.", file=sys.stderr)
        return 2
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"

    try:
        references = find_assignment_files(args.db, origin, args.student, args.course,
                                           args.all_periods, period_selector)
    except sqlite3.Error as exc:
        print(f"Could not find assignment files: {exc}", file=sys.stderr)
        return 1
    if len(references) > args.limit:
        print(f"Found {len(references)} unique files, exceeding --limit {args.limit}; narrow the query or raise the limit.", file=sys.stderr)
        return 2
    if not references:
        print("No Canvas-hosted files linked from matching assignments were found.")
        return 0
    reference_count = sum(len(items) for items in references.values())
    if args.all_periods:
        scope = "all stored periods"
    elif period_selector["academic_year"] is None:
        scope = "current quarter"
    elif period_selector["quarter_number"] is not None:
        scope = f"{period_selector['academic_year']}-Q{period_selector['quarter_number']}"
    else:
        scope = f"{period_selector['academic_year']}-S{period_selector['semester_number']}"
    print(f"Found {len(references)} unique Canvas file(s) across {reference_count} assignment link(s) ({scope}).")
    if args.dry_run:
        print("Dry run: no files downloaded. Use `canvas materials` to download them.")
        return 0
    return download_files(
        references, origin, Path("data/materials"), args.timeout * 1000,
        args.max_mb * 1024 * 1024, args.refresh, args.headed,
    )


if __name__ == "__main__":
    raise SystemExit(main())
