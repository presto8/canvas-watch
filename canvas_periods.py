"""Helpers for selecting Canvas academic quarters and semesters."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def parse_period_selector(quarter: str | None, semester: str | None) -> dict:
    if quarter:
        return _parse_period(quarter, "Q")
    if semester:
        return _parse_period(semester, "S")
    return {
        "academic_year": None,
        "grade_level": None,
        "grade_label": None,
        "quarter_number": None,
        "semester_number": None,
    }


def selector_requested(selector: dict) -> bool:
    return selector.get("academic_year") is not None or selector.get("grade_level") is not None


def students_without_graduation_year(db_path: Path, student_filter: str | None = None) -> list[str]:
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as connection:
        rows = connection.execute("""
            SELECT s.name
            FROM students AS s
            WHERE s.graduation_year IS NULL
              AND EXISTS (SELECT 1 FROM course_scrapes AS cs WHERE cs.student_id=s.id)
              AND (:student IS NULL OR lower(s.name) LIKE '%' || lower(:student) || '%')
            ORDER BY s.name
        """, {"student": student_filter}).fetchall()
    return [row[0] for row in rows]


def _parse_period(value: str, kind: str) -> dict:
    match = re.fullmatch(r"(\d{4}|K|Kindergarten|\d{1,2}(?:st|nd|rd|th)?)-" + kind + r"([1-4])", value.strip(), re.IGNORECASE)
    maximum = 4 if kind == "Q" else 2
    if not match or int(match.group(2)) > maximum:
        label = "quarter YYYY-Q1 through YYYY-Q4 or grade-Q1 through grade-Q4" if kind == "Q" else "semester YYYY-S1/S2 or grade-S1/S2"
        raise ValueError(f"Invalid period selector {value!r}; use {label}, e.g. 2020-Q1 or 10th-Q1.")

    scope, number = match.groups()
    number = int(number)
    if scope.isdigit() and len(scope) == 4:
        year_fields = {"academic_year": int(scope), "grade_level": None, "grade_label": None}
    elif scope.casefold() in {"k", "kindergarten"}:
        year_fields = {"academic_year": None, "grade_level": 0, "grade_label": "Kindergarten"}
    else:
        grade_match = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", scope, re.IGNORECASE)
        grade_level = int(grade_match.group(1))
        if not 1 <= grade_level <= 12:
            raise ValueError("Grade-level selectors must use K or grades 1–12.")
        expected = "th" if 11 <= grade_level % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(grade_level % 10, "th")
        if grade_match.group(2) and grade_match.group(2).casefold() != expected:
            raise ValueError(f"Use the correct ordinal suffix for grade {grade_level} ({grade_level}{expected}).")
        year_fields = {"academic_year": None, "grade_level": grade_level, "grade_label": f"{grade_level}{expected}"}

    return {
        **year_fields,
        "quarter_number": number if kind == "Q" else None,
        "semester_number": number if kind == "S" else None,
    }


def academic_year_start(course: dict) -> int | None:
    term = course.get("term") or {}
    name = term.get("name") or ""
    match = re.match(r"\s*(\d{4})/\d{4}", name)
    if match:
        return int(match.group(1))
    start = term.get("start_at") or course.get("start_at")
    if not start:
        return None
    try:
        start_date = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return start_date.year - 1 if start_date.month < 7 else start_date.year


def select_grading_periods(periods, course: dict, current_period_id: int | None, selector: dict,
                           graduation_year: int | None = None) -> list[dict]:
    """Select Canvas grading periods for the current period or requested school year."""
    normalized = [dict(period) for period in periods]
    academic_year = selector.get("academic_year")
    if academic_year is None:
        grade_level = selector.get("grade_level")
        if grade_level is not None:
            if graduation_year is None:
                return []
            academic_year = graduation_year + grade_level - 13
    if academic_year is None:
        return [period for period in normalized if period.get("id") == current_period_id]
    if academic_year_start(course) != academic_year:
        return []

    quarter_number = selector.get("quarter_number")
    semester_number = selector.get("semester_number")
    if quarter_number is not None:
        wanted = {quarter_number}
    elif semester_number == 1:
        wanted = {1, 2}
    else:
        wanted = {3, 4}

    result = []
    for period in normalized:
        match = re.fullmatch(r"\s*(?:Quarter\s*|Q)([1-4])\s*", period.get("title") or "", re.IGNORECASE)
        if match and int(match.group(1)) in wanted:
            result.append(period)
    return sorted(result, key=lambda period: period.get("start_at") or "")


def assignment_matches_period(assignment: dict, submission: dict, periods: list[dict]) -> bool:
    def as_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    assignment_period_id = as_int(assignment.get("grading_period_id"))
    submission_period_id = as_int(submission.get("grading_period_id"))
    due_at = assignment.get("due_at")
    due = None
    if due_at:
        try:
            due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        except (AttributeError, ValueError):
            due = None

    for period in periods:
        period_id = as_int(period.get("id"))
        if period_id is not None and period_id in {assignment_period_id, submission_period_id}:
            return True
        if due is None or not period.get("start_at") or not period.get("end_at"):
            continue
        try:
            start = datetime.fromisoformat(period["start_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(period["end_at"].replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if start <= due <= end:
                return True
        except (AttributeError, ValueError):
            continue
    return False
