#!/usr/bin/env python3
"""Show observed students and courses whose Canvas term is current."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("view", nargs="?", choices=("all", "students", "classes", "assignments", "not-submitted", "alerts", "upcoming", "history"), default="all",
                        help="Show students, classes, grades, alerts, upcoming deadlines, or recent history")
    parser.add_argument("--db", type=Path, default=Path("data/canvas.sqlite3"), help="SQLite database path")
    parser.add_argument("--student", help="Show only a student whose name contains this text")
    parser.add_argument("--course", help="Course name or ID (required for assignments view)")
    parser.add_argument("--all-periods", "--all-classes", dest="all_periods", action="store_true",
                        help="Include assignments outside the current grading period in show/missing views")
    parser.add_argument("--threshold", type=float, default=90.0, help="Low-grade percentage threshold (default: 90)")
    parser.add_argument("--days", type=int, default=7, help="Upcoming deadline window in days (default: 7)")
    parser.add_argument("--include-submitted", action="store_true", help="Include assignments already submitted or graded")
    parser.add_argument("--limit", type=int, default=10, help="Number of recent runs/changes to show (default: 10)")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(), metavar="YYYY-MM-DD",
                        help="Date used to determine current terms (default: today)")
    return parser.parse_args()


def term_dates(term: dict) -> tuple[date | None, date | None]:
    def parse(value):
        if not value:
            return None
        try:
            return date.fromisoformat(value[:10])
        except (TypeError, ValueError):
            return None

    return parse(term.get("start_at")), parse(term.get("end_at"))


def show_assignments(db_path: Path, student_query: str, course_query: str, all_periods: bool = False) -> int:
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        student_matches = connection.execute(
            "SELECT id, name FROM students WHERE lower(name) LIKE ? ORDER BY name",
            (f"%{student_query.casefold()}%",),
        ).fetchall()
        if not student_matches:
            print(f"No student matched {student_query!r}.")
            return 0
        if len(student_matches) > 1:
            print("Student name is ambiguous; use a more specific --student value:")
            for student in student_matches:
                print(f"  • {student['name']}")
            return 2
        student = student_matches[0]
        courses = connection.execute("""
            SELECT cs.course_id, cs.scrape_run_id, cs.data_status, cs.raw_course_json,
                   c.name AS course_name
            FROM course_scrapes AS cs
            JOIN courses AS c ON c.id = cs.course_id
            WHERE cs.student_id = ?
              AND cs.scrape_run_id = (
                SELECT MAX(latest.scrape_run_id)
                FROM course_scrapes AS latest
                WHERE latest.student_id = cs.student_id
              )
            ORDER BY c.name
        """, (student["id"],)).fetchall()

        query = course_query.casefold()
        matches = [course for course in courses if str(course["course_id"]) == course_query]
        if not matches:
            matches = [course for course in courses if query in (course["course_name"] or "").casefold()]
        if not matches:
            print(f"No course matched {course_query!r} for {student['name']}.")
            print("Available courses:")
            for course in courses:
                label = course["course_name"] or f"Course {course['course_id']}"
                print(f"  • {label}")
            return 0

        for course in matches:
            course_id = course["course_id"]
            run_id = course["scrape_run_id"]
            metadata = json.loads(course["raw_course_json"] or "{}")
            term = metadata.get("term") or {}
            term_label = f" — {term['name']}" if term.get("name") else ""
            course_name = course["course_name"] or f"Course {course_id}"
            period = connection.execute("""
                SELECT gp.title, gp.start_at, gp.end_at
                FROM enrollments AS e
                JOIN grading_periods AS gp
                  ON gp.scrape_run_id=e.scrape_run_id
                 AND gp.student_id=e.student_id
                 AND gp.course_id=e.course_id
                 AND gp.grading_period_id=CAST(json_extract(e.raw_json, '$.current_grading_period_id') AS INTEGER)
                WHERE e.scrape_run_id=? AND e.student_id=? AND e.course_id=?
                  AND e.enrollment_state='active'
                LIMIT 1
            """, (run_id, student["id"], course_id)).fetchone()
            period_label = ""
            if period:
                period_label = f" — {period['title']} ({(period['start_at'] or '')[:10]} to {(period['end_at'] or '')[:10]})"
            print(f"\n{student['name']} — {course_name}{term_label}{period_label}")
            if course["data_status"] == "partial":
                print("  Note: Canvas provided partial data for this course.")

            if not all_periods and not period:
                print("  Current grading period is unavailable. Run `canvas sync` to refresh, or use `--all-periods`.")
                continue

            assignment_sql = """
                SELECT a.assignment_id, a.name, a.points_possible, a.due_at,
                       sub.score, sub.grade, sub.submitted_at, sub.graded_at,
                       sub.raw_json AS submission_json
                FROM assignments AS a
                LEFT JOIN submissions AS sub
                  ON sub.scrape_run_id = a.scrape_run_id
                 AND sub.student_id = a.student_id
                 AND sub.course_id = a.course_id
                 AND sub.assignment_id = a.assignment_id
                WHERE a.scrape_run_id = ? AND a.student_id = ? AND a.course_id = ?
            """
            params = [run_id, student["id"], course_id]
            if not all_periods:
                assignment_sql += " AND datetime(a.due_at) BETWEEN datetime(?) AND datetime(?)"
                params.extend((period["start_at"], period["end_at"]))
            assignment_sql += " ORDER BY CASE WHEN a.due_at IS NULL THEN 1 ELSE 0 END, a.due_at, a.name"
            rows = connection.execute(assignment_sql, params).fetchall()
            if not rows:
                print("  No assignment records were stored for this course.")
                continue

            print(f"  {'ASSIGNMENT':48} {'GRADE':16} {'DUE':12} STATUS")
            print(f"  {'-' * 48} {'-' * 16} {'-' * 12} {'-' * 14}")
            for assignment in rows:
                submission = json.loads(assignment["submission_json"] or "{}")
                if submission.get("excused"):
                    grade_label = "Excused"
                elif assignment["score"] is None:
                    grade_label = assignment["grade"] or "Not graded"
                else:
                    score = f"{assignment['score']:g}"
                    points = assignment["points_possible"]
                    grade_label = f"{score}/{points:g}" if points is not None else score
                    if assignment["grade"] and assignment["grade"] != score:
                        grade_label += f" ({assignment['grade']})"
                if submission.get("missing"):
                    status = "Missing"
                elif submission.get("late"):
                    status = "Late"
                elif (
                    assignment["score"] is not None
                    or assignment["grade"] is not None
                    or assignment["graded_at"] is not None
                ):
                    status = "Graded"
                elif assignment["submitted_at"]:
                    status = "Submitted"
                else:
                    status = "Not submitted"
                name = (assignment["name"] or f"Assignment {assignment['assignment_id']}")[:48]
                due = (assignment["due_at"] or "")[:10] or "—"
                print(f"  {name:48} {grade_label:16} {due:12} {status}")
        return 0
    except sqlite3.Error as exc:
        print(f"Could not query database: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


def show_not_submitted(db_path: Path, student_filter: str | None = None, all_periods: bool = False) -> int:
    query_path = Path(__file__).parent / "queries" / "not_submitted.sql"
    try:
        sql = query_path.read_text(encoding="utf-8")
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error) as exc:
        print(f"Could not open not-submitted query/database: {exc}", file=sys.stderr)
        return 1

    try:
        params = {"student": student_filter, "all_periods": int(all_periods)}
        rows = connection.execute(sql, params).fetchall()
        partial_query = """
            WITH latest_course_runs AS (
                SELECT student_id, course_id, MAX(scrape_run_id) AS scrape_run_id
                FROM course_scrapes GROUP BY student_id, course_id
            )
            SELECT COUNT(*) FROM latest_course_runs AS latest
            JOIN course_scrapes AS cs
              ON cs.scrape_run_id=latest.scrape_run_id
             AND cs.student_id=latest.student_id AND cs.course_id=latest.course_id
            JOIN students AS s ON s.id=cs.student_id
            JOIN enrollments AS e
              ON e.scrape_run_id=cs.scrape_run_id
             AND e.student_id=cs.student_id AND e.course_id=cs.course_id
            LEFT JOIN grading_periods AS gp
              ON gp.scrape_run_id=e.scrape_run_id
             AND gp.student_id=e.student_id AND gp.course_id=e.course_id
             AND gp.grading_period_id=CAST(json_extract(e.raw_json, '$.current_grading_period_id') AS INTEGER)
            WHERE cs.data_status='partial'
              AND e.enrollment_state='active'
              AND (:all_periods=1 OR gp.grading_period_id IS NOT NULL)
              AND (:student IS NULL OR lower(s.name) LIKE '%' || lower(:student) || '%')
        """
        partial_count = connection.execute(partial_query, params).fetchone()[0]
    except sqlite3.Error as exc:
        print(f"Could not query not-submitted assignments: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    if not rows:
        scope = "all stored periods" if all_periods else "current-quarter assignments"
        print(f"No not-submitted assignments found in {scope}.")
    else:
        scope = "all stored periods" if all_periods else "current-quarter assignments"
        print(f"Not-submitted assignments in {scope}: {len(rows)}")
        print(f"{'STUDENT':24} {'COURSE':36} {'ASSIGNMENT':42} {'DUE':12} STATUS")
        print(f"{'-' * 24} {'-' * 36} {'-' * 42} {'-' * 12} {'-' * 14}")
        for row in rows:
            student = (row["student"] or "")[:24]
            course = (row["course"] or "")[:36]
            assignment = (row["assignment"] or "")[:42]
            due = (row["due_at"] or "")[:10] or "—"
            status = row["due_status"]
            if row["data_status"] == "partial":
                status += "*"
            print(f"{student:24} {course:36} {assignment:42} {due:12} {status}")
    if partial_count:
        print(
            f"\nNote: {partial_count} latest course record(s) have partial Canvas data; "
            "assignments hidden from the signed-in account cannot appear in this result."
        )
    return 0


def show_alerts(
    db_path: Path,
    student_filter: str | None = None,
    threshold: float = 90.0,
    all_periods: bool = False,
) -> int:
    if not 0 <= threshold <= 100:
        print("--threshold must be between 0 and 100.", file=sys.stderr)
        return 2
    query_path = Path(__file__).parent / "queries" / "below_threshold_or_missing.sql"
    try:
        sql = query_path.read_text(encoding="utf-8")
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error) as exc:
        print(f"Could not open alerts query/database: {exc}", file=sys.stderr)
        return 1

    try:
        rows = connection.execute(sql, {
            "student": student_filter,
            "threshold": threshold,
            "all_periods": int(all_periods),
        }).fetchall()
    except sqlite3.Error as exc:
        print(f"Could not query low-grade/missing assignments: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    scope = "all stored periods" if all_periods else "current quarter"
    if not rows:
        print(f"No assignments below {threshold:g}% or missing in the {scope}.")
        return 0

    print(f"Assignments below {threshold:g}% or missing in the {scope}: {len(rows)}")
    print(f"{'STUDENT':24} {'COURSE':36} {'ASSIGNMENT':42} {'SCORE':14} {'PERCENT':10} {'DUE':12} FLAG")
    print(f"{'-' * 24} {'-' * 36} {'-' * 42} {'-' * 14} {'-' * 10} {'-' * 12} {'-' * 18}")
    for row in rows:
        student = (row["student"] or "")[:24]
        course = (row["course"] or "")[:36]
        assignment = (row["assignment"] or "")[:42]
        score = "—"
        if row["score"] is not None:
            score = f"{row['score']:g}"
            if row["points_possible"] is not None:
                score += f"/{row['points_possible']:g}"
        elif row["grade"]:
            score = str(row["grade"])
        percentage = f"{row['percentage']:.1f}%" if row["percentage"] is not None else "—"
        due = (row["due_at"] or "")[:10] or "—"
        flag = row["reason"]
        if row["data_status"] == "partial":
            flag += "*"
        print(f"{student:24} {course:36} {assignment:42} {score:14} {percentage:10} {due:12} {flag}")
    print("\nPercentages use Canvas score ÷ points possible. Letter-only grades without numeric scores may not be comparable.")
    return 0


def show_upcoming(
    db_path: Path,
    student_filter: str | None = None,
    days: int = 7,
    all_periods: bool = False,
    include_submitted: bool = False,
) -> int:
    if not 1 <= days <= 365:
        print("--days must be between 1 and 365.", file=sys.stderr)
        return 2
    query_path = Path(__file__).parent / "queries" / "upcoming.sql"
    try:
        sql = query_path.read_text(encoding="utf-8")
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error) as exc:
        print(f"Could not open upcoming query/database: {exc}", file=sys.stderr)
        return 1

    try:
        rows = connection.execute(sql, {
            "student": student_filter,
            "days": days,
            "all_periods": int(all_periods),
            "include_submitted": int(include_submitted),
        }).fetchall()
    except sqlite3.Error as exc:
        print(f"Could not query upcoming deadlines: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    scope = "all stored periods" if all_periods else "current quarter"
    submission_scope = "all assignments" if include_submitted else "not-yet-submitted assignments"
    if not rows:
        print(f"No {submission_scope} due in the next {days} days in the {scope}.")
        return 0

    print(f"Upcoming deadlines in the next {days} days ({scope}): {len(rows)}")
    print(f"{'STUDENT':24} {'COURSE':36} {'ASSIGNMENT':42} {'DUE (LOCAL)':25} STATUS")
    print(f"{'-' * 24} {'-' * 36} {'-' * 42} {'-' * 25} {'-' * 14}")
    for row in rows:
        student = (row["student"] or "")[:24]
        course = (row["course"] or "")[:36]
        assignment = (row["assignment"] or "")[:42]
        due = "—"
        if row["due_at"]:
            try:
                due_at = datetime.fromisoformat(row["due_at"].replace("Z", "+00:00"))
                if row["time_zone"]:
                    try:
                        due_at = due_at.astimezone(ZoneInfo(row["time_zone"]))
                    except ZoneInfoNotFoundError:
                        pass
                due = due_at.strftime("%Y-%m-%d %H:%M %Z").strip()
            except ValueError:
                due = row["due_at"][:16].replace("T", " ")
        status = row["status"]
        if row["data_status"] == "partial":
            status += "*"
        print(f"{student:24} {course:36} {assignment:42} {due:25} {status}")
    return 0


def show_history(db_path: Path, student_filter: str | None = None, limit: int = 10) -> int:
    if not 1 <= limit <= 100:
        print("--limit must be between 1 and 100.", file=sys.stderr)
        return 2
    try:
        connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"Could not open database: {exc}", file=sys.stderr)
        return 1

    try:
        runs = connection.execute("""
            SELECT r.id, r.scraped_at, r.source,
                   COUNT(DISTINCT cs.student_id) AS students,
                   COUNT(cs.course_id) AS courses,
                   COALESCE(SUM(cs.assignment_count), 0) AS assignments,
                   COALESCE(SUM(cs.submission_count), 0) AS submissions
            FROM scrape_runs AS r
            LEFT JOIN course_scrapes AS cs ON cs.scrape_run_id = r.id
            LEFT JOIN students AS s ON s.id = cs.student_id
            WHERE (:student IS NULL OR lower(s.name) LIKE '%' || lower(:student) || '%')
            GROUP BY r.id
            ORDER BY r.id DESC
            LIMIT :limit
        """, {"student": student_filter, "limit": limit}).fetchall()

        changes = connection.execute("""
            WITH ranked AS (
                SELECT sub.*,
                       r.scraped_at,
                       LAG(sub.scrape_run_id) OVER w AS previous_run_id,
                       LAG(sub.score) OVER w AS previous_score,
                       LAG(sub.grade) OVER w AS previous_grade,
                       LAG(sub.submitted_at) OVER w AS previous_submitted_at,
                       LAG(sub.graded_at) OVER w AS previous_graded_at,
                       json_extract(sub.raw_json, '$.missing') AS missing_flag,
                       json_extract(sub.raw_json, '$.late') AS late_flag,
                       LAG(json_extract(sub.raw_json, '$.missing')) OVER w AS previous_missing,
                       LAG(json_extract(sub.raw_json, '$.late')) OVER w AS previous_late
                FROM submissions AS sub
                JOIN scrape_runs AS r ON r.id = sub.scrape_run_id
                JOIN students AS s ON s.id = sub.student_id
                WHERE (:student IS NULL OR lower(s.name) LIKE '%' || lower(:student) || '%')
                WINDOW w AS (
                    PARTITION BY sub.student_id, sub.course_id, sub.assignment_id
                    ORDER BY sub.scrape_run_id
                )
            )
            SELECT ranked.*, s.name AS student, c.name AS course,
                   a.name AS assignment, a.points_possible
            FROM ranked
            JOIN students AS s ON s.id = ranked.student_id
            LEFT JOIN courses AS c ON c.id = ranked.course_id
            LEFT JOIN assignments AS a
              ON a.scrape_run_id = ranked.scrape_run_id
             AND a.student_id = ranked.student_id
             AND a.course_id = ranked.course_id
             AND a.assignment_id = ranked.assignment_id
            WHERE previous_run_id IS NOT NULL
              AND (
                  score IS NOT previous_score
                  OR grade IS NOT previous_grade
                  OR submitted_at IS NOT previous_submitted_at
                  OR graded_at IS NOT previous_graded_at
                  OR json_extract(ranked.raw_json, '$.missing') IS NOT previous_missing
                  OR json_extract(ranked.raw_json, '$.late') IS NOT previous_late
              )
            ORDER BY ranked.scrape_run_id DESC, student, course, assignment
            LIMIT :limit
        """, {"student": student_filter, "limit": limit}).fetchall()
    except sqlite3.Error as exc:
        print(f"Could not query history: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    if student_filter and not runs:
        print(f"No sync history found for student matching {student_filter!r}.")
        return 0

    print("Recent syncs")
    if not runs:
        print("  No sync runs recorded yet.")
    else:
        for run in runs:
            print(
                f"  #{run['id']}  {run['scraped_at']}  "
                f"{run['students']} students, {run['courses']} courses, "
                f"{run['assignments']} assignments, {run['submissions']} submissions"
            )

    print("\nRecent grade/submission changes")
    if not changes:
        print("  No grade or submission changes detected between stored syncs.")
        return 0
    for change in changes:
        before = change["previous_grade"]
        if change["previous_score"] is not None:
            before = f"{change['previous_score']:g}"
            if change["points_possible"] is not None:
                before += f"/{change['points_possible']:g}"
        before = before or "—"
        after = change["grade"]
        if change["score"] is not None:
            after = f"{change['score']:g}"
            if change["points_possible"] is not None:
                after += f"/{change['points_possible']:g}"
        after = after or "—"
        details = []
        if change["score"] != change["previous_score"] or change["grade"] != change["previous_grade"]:
            details.append(f"grade {before} → {after}")
        if change["submitted_at"] != change["previous_submitted_at"]:
            details.append("submission updated")
        if change["graded_at"] != change["previous_graded_at"]:
            details.append("grading updated")
        if change["missing_flag"] != change["previous_missing"]:
            details.append("missing status changed")
        if change["late_flag"] != change["previous_late"]:
            details.append("late status changed")
        assignment_name = change["assignment"] or f"Assignment {change['assignment_id']}"
        print(
            f"  {change['scraped_at']}  {change['student']} — {change['course'] or 'Course'} — "
            f"{assignment_name}: {', '.join(details)}"
        )
    return 0


def main() -> int:
    args = parse_args()
    if not args.db.is_file():
        print(f"Database not found: {args.db}. Run canvas_scraper.py first.", file=sys.stderr)
        return 2

    if args.view == "assignments":
        if not args.student or not args.course:
            print("The assignments view requires both --student and --course.", file=sys.stderr)
            return 2
        try:
            return show_assignments(args.db, args.student, args.course)
        except sqlite3.Error as exc:
            print(f"Could not open database: {exc}", file=sys.stderr)
            return 1
    if args.view == "not-submitted":
        return show_not_submitted(args.db, args.student, args.all_periods)
    if args.view == "alerts":
        return show_alerts(args.db, args.student, args.threshold, args.all_periods)
    if args.view == "upcoming":
        return show_upcoming(args.db, args.student, args.days, args.all_periods, args.include_submitted)
    if args.view == "history":
        return show_history(args.db, args.student, args.limit)

    try:
        connection = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"Could not open database: {exc}", file=sys.stderr)
        return 1

    try:
        rows = connection.execute("""
            SELECT s.id AS student_id, s.name AS student_name,
                   cs.course_id, cs.raw_course_json, cs.data_status,
                   e.enrollment_state
            FROM course_scrapes AS cs
            JOIN students AS s ON s.id = cs.student_id
            LEFT JOIN enrollments AS e
              ON e.scrape_run_id = cs.scrape_run_id
             AND e.student_id = cs.student_id
             AND e.course_id = cs.course_id
            WHERE cs.scrape_run_id = (
                SELECT MAX(latest.scrape_run_id)
                FROM course_scrapes AS latest
                WHERE latest.student_id = cs.student_id
            )
            ORDER BY s.name, cs.course_id
        """).fetchall()
    except sqlite3.Error as exc:
        print(f"Could not query database: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    students: dict[int, dict] = {}
    filter_text = (args.student or "").casefold()
    for row in rows:
        name = row["student_name"] or f"Student {row['student_id']}"
        if filter_text and filter_text not in name.casefold():
            continue
        student = students.setdefault(row["student_id"], {"name": name, "current": [], "undated": []})
        if row["enrollment_state"] != "active":
            continue
        try:
            course = json.loads(row["raw_course_json"])
        except (TypeError, json.JSONDecodeError):
            course = {}
        term = course.get("term") or {}
        start, end = term_dates(term)
        details = {
            "name": course.get("name") or f"Course {row['course_id']} (name unavailable)",
            "term": term.get("name"),
            "status": row["data_status"],
        }
        if start is not None and end is not None:
            if start <= args.as_of <= end:
                student["current"].append(details)
        else:
            student["undated"].append(details)

    if not students:
        if filter_text:
            print(f"No student matched {args.student!r} in the latest stored scrape.")
        else:
            print("No student records found. Run canvas_scraper.py first.")
        return 0

    if args.view == "students":
        print("Observed students")
        for student in students.values():
            print(f"  • {student['name']} ({len(student['current'])} current classes)")
        return 0

    print(f"Students and current classes as of {args.as_of.isoformat()}")
    for student in students.values():
        print(f"\n{student['name']}")
        if student["current"]:
            for course in sorted(student["current"], key=lambda item: item["name"].casefold()):
                suffix = f" — {course['term']}" if course["term"] else ""
                if course["status"] == "partial":
                    suffix += " [partial Canvas data]"
                print(f"  • {course['name']}{suffix}")
        else:
            print("  (no classes with term dates covering this date)")
        if student["undated"]:
            print("  Active courses without term dates (not classified as current):")
            for course in sorted(student["undated"], key=lambda item: item["name"].casefold()):
                suffix = f" — {course['term']}" if course["term"] else ""
                print(f"    • {course['name']}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
