"""Helpers for selecting Canvas academic quarters and semesters."""

from __future__ import annotations

import re
from datetime import datetime, timezone


def parse_period_selector(quarter: str | None, semester: str | None) -> dict:
    if quarter:
        match = re.fullmatch(r"(\d{4})-Q([1-4])", quarter.strip(), re.IGNORECASE)
        if not match:
            raise ValueError("Quarter must use the format YYYY-Q1 through YYYY-Q4, e.g. 2020-Q1.")
        return {"academic_year": int(match.group(1)), "quarter_number": int(match.group(2)), "semester_number": None}
    if semester:
        match = re.fullmatch(r"(\d{4})-S([1-2])", semester.strip(), re.IGNORECASE)
        if not match:
            raise ValueError("Semester must use the format YYYY-S1 or YYYY-S2, e.g. 2020-S1.")
        return {"academic_year": int(match.group(1)), "quarter_number": None, "semester_number": int(match.group(2))}
    return {"academic_year": None, "quarter_number": None, "semester_number": None}


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


def select_grading_periods(periods, course: dict, current_period_id: int | None, selector: dict) -> list[dict]:
    """Select Canvas grading periods for the current period or requested school year."""
    normalized = [dict(period) for period in periods]
    academic_year = selector.get("academic_year")
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
