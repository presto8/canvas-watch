#!/usr/bin/env python3
"""Export observed students' course grades, or scrape one Canvas page."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="Scrape this one Canvas page instead of all observed students")
    parser.add_argument("--page", action="store_true", help="Scrape CANVAS_URL as one page")
    parser.add_argument("--students", nargs="+", help="Filter observed students by name (default: all observees)")
    parser.add_argument("--db", type=Path, default=Path("data/canvas.sqlite3"), help="SQLite database path")
    parser.add_argument("--json", action="store_true", help="Also write a timestamped JSON snapshot")
    parser.add_argument("--output", type=Path, help="Write a JSON snapshot to this path (also enables --json)")
    parser.add_argument("--headed", action="store_true", help="Show the browser for interactive sign-in/MFA")
    parser.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds (default: 30)")
    return parser.parse_args()


def login_if_needed(page, username: str, password: str, timeout_ms: int, headed: bool) -> bool:
    """Submit the standard Canvas login form when it is present."""
    user_field = page.locator(
        'input#pseudonym_session_unique_id, '
        'input[name="pseudonym_session[unique_id]"], '
        'input[autocomplete="username"]'
    ).first
    try:
        user_field.wait_for(state="visible", timeout=2500)
    except PlaywrightTimeoutError:
        return False

    password_field = page.locator(
        'input#pseudonym_session_password, '
        'input[name="pseudonym_session[password]"], '
        'input[type="password"]'
    ).first
    if not password_field.is_visible():
        raise RuntimeError("Canvas sign-in page appeared, but no password field was found.")

    user_field.fill(username)
    password_field.fill(password)
    submit = page.locator(
        'button[type="submit"], input[type="submit"], button:has-text("Log In")'
    ).first
    submit.click()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        # Some sign-in flows update the page without a full navigation.
        pass

    try:
        user_field.wait_for(state="visible", timeout=2500)
    except PlaywrightTimeoutError:
        return True
    if not headed:
        raise RuntimeError(
            "Canvas is still showing a sign-in form. Check the credentials or "
            "use --headed to complete an interactive sign-in/MFA step."
        )
    return True


def scrape_page(page) -> dict:
    """Collect visible text, tables, and links from the rendered document."""
    return page.evaluate(
        """() => {
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const tables = Array.from(document.querySelectorAll('table')).filter(visible).map((table) =>
            Array.from(table.querySelectorAll('tr')).map((row) =>
              Array.from(row.querySelectorAll('th, td')).map((cell) => clean(cell.innerText))
            ).filter((row) => row.length)
          );
          const links = Array.from(document.querySelectorAll('a[href]')).filter(visible).map((a) => ({
            text: clean(a.innerText),
            href: a.href
          })).filter((a) => a.text || a.href);
          return {
            title: document.title,
            url: location.href,
            text: document.body ? document.body.innerText.trim() : '',
            tables,
            links
          };
        }"""
    )


def scrape_observed_students(page, student_queries: list[str] | None) -> dict:
    """Use Canvas's authenticated API to collect observees' courses and grade data."""
    return page.evaluate(
        """async (studentQueries) => {
          const api = async (path) => {
            let url = new URL(path, location.origin).href;
            const all = [];
            while (url) {
              const response = await fetch(url, {
                credentials: 'same-origin',
                headers: {Accept: 'application/json'}
              });
              if (!response.ok) throw new Error(`Canvas API returned HTTP ${response.status} for ${path}`);
              const data = await response.json();
              if (!Array.isArray(data)) throw new Error(`Canvas API returned an unexpected response for ${path}`);
              all.push(...data);
              const link = response.headers.get('Link') || '';
              const next = link.match(/<([^>]+)>;\\s*rel="next"/);
              url = next ? next[1] : null;
            }
            return all;
          };
          const normalize = (value) => value.toLocaleLowerCase().replace(/[^a-z0-9]/g, '').replace(/(.)\\1+/g, '$1');
          const observees = await api('/api/v1/users/self/observees?per_page=100');
          let selected = observees;
          if (studentQueries && studentQueries.length) {
            const queries = studentQueries.map(normalize);
            selected = observees.filter((person) => {
              const name = normalize(person.name || '');
              return queries.some((query) => name.includes(query));
            });
            const missing = studentQueries.filter((query) =>
              !selected.some((person) => normalize(person.name || '').includes(normalize(query)))
            );
            if (missing.length) throw new Error(`No observed Canvas student matched: ${missing.join(', ')}`);
          }
          if (!selected.length) throw new Error('No observed students are available to this Canvas account.');

          const people = await Promise.all(selected.map(async (person) => {
            const courses = await api(
              `/api/v1/users/${person.id}/courses?enrollment_state=all&include[]=term&include[]=total_scores&include[]=current_grading_period_scores&per_page=100`
            );
            return {person, courses};
          }));

          const tasks = [];
          for (const {person, courses} of people) {
            for (const course of courses) tasks.push({person, course});
          }
          const details = new Map();
          let cursor = 0;
          const worker = async () => {
            while (cursor < tasks.length) {
              const task = tasks[cursor++];
              const {person, course} = task;
              const key = `${person.id}:${course.id}`;
              const query = `student_ids[]=${person.id}&include[]=assignment&per_page=100`;
              const commentsQuery = `${query}&include[]=submission_comments&include[]=rubric_assessment`;
              const hasGradingPeriods = course.has_grading_periods === true || (course.enrollments || []).some(
                (enrollment) => enrollment.current_grading_period_id != null
              );
              const gradingPeriodsRequest = hasGradingPeriods
                ? (async () => {
                    const response = await fetch(
                      `/api/v1/courses/${course.id}/grading_periods?per_page=100`,
                      {credentials: 'same-origin', headers: {Accept: 'application/json'}}
                    );
                    if (!response.ok) throw new Error(`Canvas API returned HTTP ${response.status} for grading periods`);
                    const payload = await response.json();
                    return Array.isArray(payload) ? payload : (payload.grading_periods || []);
                  })()
                : Promise.resolve([]);
              const [assignments, submissionResult, gradingPeriods] = await Promise.all([
                api(`/api/v1/courses/${course.id}/assignments?per_page=100`).catch((error) => ({error: String(error)})),
                (async () => {
                  try {
                    return await api(`/api/v1/courses/${course.id}/students/submissions?${commentsQuery}`);
                  } catch (error) {
                    try {
                      return await api(`/api/v1/courses/${course.id}/students/submissions?${query}`);
                    } catch (fallbackError) {
                      return {error: String(fallbackError)};
                    }
                  }
                })(),
                gradingPeriodsRequest.catch((error) => ({error: String(error)}))
              ]);
              const result = {data_status: course.name ? 'available' : 'partial'};
              if (Array.isArray(gradingPeriods)) result.grading_periods = gradingPeriods;
              else result.grading_periods_error = gradingPeriods.error;
              if (Array.isArray(submissionResult)) result.submissions = submissionResult;
              else {
                result.submissions = [];
                result.submissions_error = submissionResult.error;
                result.data_status = 'partial';
              }
              if (!course.name) result.course_metadata_restricted = true;
              if (Array.isArray(assignments)) {
                result.assignments = assignments;
              } else {
                result.assignments_error = assignments.error;
                const embedded = new Map();
                for (const submission of result.submissions) {
                  if (submission.assignment) embedded.set(submission.assignment.id, submission.assignment);
                }
                result.assignments = Array.from(embedded.values());
                result.assignments_source = 'embedded in student submissions';
                result.data_status = 'partial';
              }
              details.set(key, result);
            }
          };
          await Promise.all(Array.from({length: Math.min(4, tasks.length)}, worker));

          return {
            source: 'Canvas authenticated API',
            students: people.map(({person, courses}) => ({
              student: person,
              courses: courses.map((course) => ({
                course,
                ...details.get(`${person.id}:${course.id}`)
              }))
            }))
          };
        }""",
        student_queries or [],
    )


def default_output(url: str) -> Path:
    course_match = re.search(r"/courses/(\d+)", urlparse(url).path)
    course = f"course-{course_match.group(1)}" if course_match else "canvas-page"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("data") / f"{course}-{timestamp}.json"


def default_observees_output() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("data") / f"canvas-observees-{timestamp}.json"


def store_observed_data(data: dict, db_path: Path) -> int:
    """Append one scrape snapshot to SQLite and return its run ID."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    previous_umask = os.umask(0o177)
    try:
        connection = sqlite3.connect(db_path)
    except Exception:
        os.umask(previous_umask)
        raise

    def dump(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS scrape_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scraped_at TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY,
                name TEXT,
                short_name TEXT,
                sortable_name TEXT,
                graduation_year INTEGER,
                raw_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY,
                name TEXT,
                course_code TEXT,
                term_name TEXT,
                workflow_state TEXT,
                raw_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS course_scrapes (
                scrape_run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
                student_id INTEGER NOT NULL REFERENCES students(id),
                course_id INTEGER NOT NULL REFERENCES courses(id),
                data_status TEXT,
                course_metadata_restricted INTEGER NOT NULL DEFAULT 0,
                assignments_error TEXT,
                submissions_error TEXT,
                assignment_count INTEGER NOT NULL DEFAULT 0,
                submission_count INTEGER NOT NULL DEFAULT 0,
                raw_course_json TEXT NOT NULL,
                PRIMARY KEY (scrape_run_id, student_id, course_id)
            );
            CREATE TABLE IF NOT EXISTS enrollments (
                scrape_run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
                student_id INTEGER NOT NULL REFERENCES students(id),
                course_id INTEGER NOT NULL REFERENCES courses(id),
                role TEXT,
                enrollment_state TEXT,
                current_grade TEXT,
                current_score REAL,
                final_grade TEXT,
                final_score REAL,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (scrape_run_id, student_id, course_id, role)
            );
            CREATE TABLE IF NOT EXISTS grading_periods (
                scrape_run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
                student_id INTEGER NOT NULL REFERENCES students(id),
                course_id INTEGER NOT NULL REFERENCES courses(id),
                grading_period_id INTEGER NOT NULL,
                title TEXT,
                start_at TEXT,
                end_at TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (scrape_run_id, student_id, course_id, grading_period_id)
            );
            CREATE TABLE IF NOT EXISTS assignments (
                scrape_run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
                student_id INTEGER NOT NULL REFERENCES students(id),
                course_id INTEGER NOT NULL REFERENCES courses(id),
                assignment_id INTEGER NOT NULL,
                name TEXT,
                points_possible REAL,
                due_at TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (scrape_run_id, student_id, course_id, assignment_id)
            );
            CREATE TABLE IF NOT EXISTS submissions (
                scrape_run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
                student_id INTEGER NOT NULL REFERENCES students(id),
                course_id INTEGER NOT NULL REFERENCES courses(id),
                record_key TEXT NOT NULL,
                assignment_id INTEGER,
                submission_id INTEGER,
                score REAL,
                grade TEXT,
                submitted_at TEXT,
                graded_at TEXT,
                workflow_state TEXT,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (scrape_run_id, student_id, course_id, record_key)
            );
            CREATE INDEX IF NOT EXISTS idx_course_scrapes_student_course
                ON course_scrapes(student_id, course_id, scrape_run_id);
            CREATE INDEX IF NOT EXISTS idx_submissions_student_assignment
                ON submissions(student_id, assignment_id, scrape_run_id);
        """)
        student_columns = {row[1] for row in connection.execute("PRAGMA table_info(students)")}
        if "graduation_year" not in student_columns:
            connection.execute("ALTER TABLE students ADD COLUMN graduation_year INTEGER")
        with connection:
            cursor = connection.execute(
                "INSERT INTO scrape_runs (scraped_at, source) VALUES (?, ?)",
                (data.get("scraped_at") or datetime.now(timezone.utc).isoformat(), data.get("source", "Canvas")),
            )
            run_id = cursor.lastrowid
            scraped_at = data.get("scraped_at") or datetime.now(timezone.utc).isoformat()

            for student_record in data.get("students", []):
                student = student_record.get("student", {})
                student_id = student.get("id")
                if student_id is None:
                    continue
                connection.execute("""
                    INSERT INTO students (id, name, short_name, sortable_name, raw_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name, short_name=excluded.short_name,
                        sortable_name=excluded.sortable_name, raw_json=excluded.raw_json,
                        updated_at=excluded.updated_at
                """, (student_id, student.get("name"), student.get("short_name"),
                      student.get("sortable_name"), dump(student), scraped_at))

                for course_record in student_record.get("courses", []):
                    course = course_record.get("course", {})
                    course_id = course.get("id")
                    if course_id is None:
                        continue
                    term = course.get("term")
                    term_name = term.get("name") if isinstance(term, dict) else None
                    connection.execute("""
                        INSERT INTO courses (id, name, course_code, term_name, workflow_state, raw_json, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            name=excluded.name, course_code=excluded.course_code,
                            term_name=excluded.term_name, workflow_state=excluded.workflow_state,
                            raw_json=excluded.raw_json, updated_at=excluded.updated_at
                    """, (course_id, course.get("name"), course.get("course_code"), term_name,
                          course.get("workflow_state"), dump(course), scraped_at))

                    assignments = course_record.get("assignments", [])
                    submissions = course_record.get("submissions", [])
                    connection.execute("""
                        INSERT INTO course_scrapes (
                            scrape_run_id, student_id, course_id, data_status,
                            course_metadata_restricted, assignments_error, submissions_error,
                            assignment_count, submission_count, raw_course_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (run_id, student_id, course_id, course_record.get("data_status"),
                          int(bool(course_record.get("course_metadata_restricted"))),
                          course_record.get("assignments_error"), course_record.get("submissions_error"),
                          len(assignments), len(submissions), dump(course)))

                    for period in course_record.get("grading_periods", []):
                        period_id = period.get("id")
                        if period_id is None:
                            continue
                        connection.execute("""
                            INSERT OR REPLACE INTO grading_periods (
                                scrape_run_id, student_id, course_id, grading_period_id,
                                title, start_at, end_at, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (run_id, student_id, course_id, period_id,
                              period.get("title") or period.get("name"),
                              period.get("start_date") or period.get("start_at"),
                              period.get("end_date") or period.get("end_at"), dump(period)))

                    for enrollment in course.get("enrollments", []):
                        role = enrollment.get("role") or enrollment.get("type") or "unknown"
                        connection.execute("""
                            INSERT OR REPLACE INTO enrollments (
                                scrape_run_id, student_id, course_id, role, enrollment_state,
                                current_grade, current_score, final_grade, final_score, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (run_id, student_id, course_id, role, enrollment.get("enrollment_state"),
                              enrollment.get("computed_current_grade"), enrollment.get("computed_current_score"),
                              enrollment.get("computed_final_grade"), enrollment.get("computed_final_score"),
                              dump(enrollment)))

                    for assignment in assignments:
                        assignment_id = assignment.get("id")
                        if assignment_id is None:
                            continue
                        connection.execute("""
                            INSERT OR REPLACE INTO assignments (
                                scrape_run_id, student_id, course_id, assignment_id,
                                name, points_possible, due_at, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (run_id, student_id, course_id, assignment_id, assignment.get("name"),
                              assignment.get("points_possible"), assignment.get("due_at"), dump(assignment)))

                    for index, submission in enumerate(submissions):
                        assignment_id = submission.get("assignment_id")
                        if assignment_id is None and isinstance(submission.get("assignment"), dict):
                            assignment_id = submission["assignment"].get("id")
                        submission_id = submission.get("id")
                        record_key = str(assignment_id or f"submission-{submission_id or index}")
                        connection.execute("""
                            INSERT OR REPLACE INTO submissions (
                                scrape_run_id, student_id, course_id, record_key, assignment_id,
                                submission_id, score, grade, submitted_at, graded_at, workflow_state, raw_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (run_id, student_id, course_id, record_key, assignment_id, submission_id,
                              submission.get("score"), submission.get("grade"), submission.get("submitted_at"),
                              submission.get("graded_at"), submission.get("workflow_state"), dump(submission)))
        return run_id
    finally:
        try:
            connection.close()
            if db_path.exists():
                os.chmod(db_path, 0o600)
        finally:
            os.umask(previous_umask)


def main() -> int:
    load_dotenv()
    args = parse_args()
    url = args.url or os.getenv("CANVAS_URL")
    page_mode = args.page or bool(args.url)
    username = os.getenv("CANVAS_USERNAME", "")
    password = os.getenv("CANVAS_PASSWORD", "")

    if not username or not password:
        print("Set CANVAS_USERNAME and CANVAS_PASSWORD in .env before running.", file=sys.stderr)
        return 2
    if not url:
        print("Set CANVAS_URL in .env or pass a Canvas page URL.", file=sys.stderr)
        return 2
    if urlparse(url).scheme != "https":
        print("Refusing to send credentials to a non-HTTPS URL.", file=sys.stderr)
        return 2

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            page = browser.new_page()
            page.set_default_timeout(args.timeout * 1000)
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            attempted_login = login_if_needed(page, username, password, args.timeout * 1000, args.headed)
            if args.headed and attempted_login:
                input("Complete any sign-in or MFA prompts in Chromium, then press Enter here...")
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            page.locator("body").wait_for(state="visible")
            if page.locator(
                'input#pseudonym_session_unique_id, input[name="pseudonym_session[unique_id]"]'
            ).count():
                raise RuntimeError("The page is still at the Canvas sign-in screen; sign-in did not complete.")
            result = scrape_page(page) if page_mode else scrape_observed_students(page, args.students)
            browser.close()
    except Exception as exc:
        print(f"Scrape failed: {exc}", file=sys.stderr)
        return 1

    result["scraped_at"] = datetime.now(timezone.utc).isoformat()
    if page_mode:
        output = args.output or default_output(url)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(output, 0o600)
        print(f"Saved Canvas page data to {output}")
    else:
        try:
            run_id = store_observed_data(result, args.db)
        except Exception as exc:
            print(f"Could not save Canvas data to SQLite: {exc}", file=sys.stderr)
            return 1
        summary = ", ".join(
            f"{item['student'].get('name', 'student')}: {len(item['courses'])} courses"
            for item in result['students']
        )
        print(f"Saved Canvas data for {summary} to {args.db} (scrape run {run_id})")
        if args.json or args.output:
            output = args.output or default_observees_output()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.chmod(output, 0o600)
            print(f"Also saved JSON snapshot to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
