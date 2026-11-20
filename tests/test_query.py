import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from canvas_query import show_alerts, show_assignments, show_history, show_not_submitted, show_upcoming, teacher_comments
from canvas_materials import canvas_file_ids
from canvas_scraper import store_observed_data


class AssignmentQueryTests(unittest.TestCase):
    def test_teacher_comments_excludes_student_authored_replies(self):
        comments = teacher_comments({
            "user_id": 7,
            "submission_comments": [
                {"author_id": 7, "author_name": "Student", "comment": "My reply"},
                {"author_id": 9, "author_name": "Teacher", "comment": "<p>Revise &amp; resubmit.</p>"},
            ],
        }, 7)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "Teacher")
        self.assertEqual(comments[0]["text"], "Revise & resubmit.")

    def test_assignment_material_links_only_include_canvas_hosted_files(self):
        assignment = {
            "description": (
                '<a href="/courses/12/files/34/download?wrap=1">handout</a>'
                '<a data-api-endpoint="/api/v1/courses/12/files/56">slides</a>'
                '<img src="https://files.example.org/files/78/image.png">'
            )
        }
        self.assertEqual(canvas_file_ids(assignment, "https://canvas.example.edu"), {34, 56})

    def test_show_assignments_includes_grade_and_status(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "canvas.sqlite3"
            store_observed_data({
                "scraped_at": "2026-01-01T00:00:00+00:00",
                "source": "Canvas authenticated API",
                "students": [{
                    "student": {"id": 7, "name": "Test Student"},
                    "courses": [{
                        "course": {
                            "id": 11,
                            "name": "Test Course",
                            "term": {"name": "Test Term"},
                            "enrollments": [{
                                "role": "ObserverEnrollment",
                                "enrollment_state": "active",
                                "current_grading_period_id": 5,
                            }],
                        },
                        "data_status": "available",
                        "grading_periods": [{
                            "id": 5,
                            "title": "Quarter 2",
                            "start_date": "2026-01-01T00:00:00Z",
                            "end_date": "2026-01-31T23:59:59Z",
                        }],
                        "assignments": [{
                            "id": 101,
                            "name": "Unit Quiz",
                            "points_possible": 10,
                            "due_at": "2026-01-10T00:00:00Z",
                        }],
                        "submissions": [{
                            "id": 201,
                            "assignment_id": 101,
                            "score": 9,
                            "grade": "9",
                            "graded_at": "2026-01-09T00:00:00Z",
                            "raw_json": "{}",
                        }],
                    }],
                }],
            }, database)

            output = io.StringIO()
            with redirect_stdout(output):
                result = show_assignments(database, "Test Student", "Test Course")

            self.assertEqual(result, 0)
            self.assertIn("Test Student — Test Course", output.getvalue())
            self.assertIn("Unit Quiz", output.getvalue())
            self.assertIn("9/10", output.getvalue())
            self.assertIn("Graded", output.getvalue())
            self.assertNotIn("Not submitted", output.getvalue())

    def test_not_submitted_query_excludes_graded_work(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "canvas.sqlite3"
            store_observed_data({
                "scraped_at": "2026-01-01T00:00:00+00:00",
                "source": "Canvas authenticated API",
                "students": [{
                    "student": {"id": 7, "name": "Test Student"},
                    "courses": [{
                        "course": {
                            "id": 11,
                            "name": "Test Course",
                            "term": {
                                "start_at": "2020-01-01T00:00:00Z",
                                "end_at": "2099-12-31T23:59:59Z",
                            },
                            "enrollments": [{
                                "role": "ObserverEnrollment",
                                "enrollment_state": "active",
                                "current_grading_period_id": 7,
                            }],
                        },
                        "data_status": "available",
                        "grading_periods": [{
                            "id": 7,
                            "title": "Test Quarter",
                            "start_date": "2020-01-01T00:00:00Z",
                            "end_date": "2099-12-31T23:59:59Z",
                        }],
                        "assignments": [
                            {"id": 101, "name": "Outstanding Work", "due_at": "2020-01-01T00:00:00Z"},
                            {"id": 102, "name": "Already Graded", "due_at": "2020-01-01T00:00:00Z"},
                        ],
                        "submissions": [{
                            "id": 202,
                            "assignment_id": 102,
                            "score": 10,
                            "grade": "10",
                            "graded_at": "2020-01-02T00:00:00Z",
                        }],
                    }],
                }],
            }, database)

            output = io.StringIO()
            with redirect_stdout(output):
                result = show_not_submitted(database)

            self.assertEqual(result, 0)
            self.assertIn("Outstanding Work", output.getvalue())
            self.assertIn("Overdue", output.getvalue())
            self.assertNotIn("Already Graded", output.getvalue())

    def test_alert_query_finds_low_scores_and_missing_work(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "canvas.sqlite3"
            store_observed_data({
                "scraped_at": "2026-01-01T00:00:00+00:00",
                "source": "Canvas authenticated API",
                "students": [{
                    "student": {"id": 7, "name": "Test Student"},
                    "courses": [{
                        "course": {
                            "id": 11,
                            "name": "Test Course",
                            "enrollments": [{
                                "role": "ObserverEnrollment",
                                "enrollment_state": "active",
                                "current_grading_period_id": 7,
                            }],
                        },
                        "data_status": "available",
                        "grading_periods": [{
                            "id": 7,
                            "title": "Test Quarter",
                            "start_date": "2020-01-01T00:00:00Z",
                            "end_date": "2099-12-31T23:59:59Z",
                        }],
                        "assignments": [
                            {"id": 101, "name": "Low Score", "points_possible": 10, "due_at": "2026-01-10T00:00:00Z"},
                            {"id": 102, "name": "Missing Work", "points_possible": 10, "due_at": "2026-01-11T00:00:00Z"},
                            {"id": 103, "name": "Good Score", "points_possible": 100, "due_at": "2026-01-12T00:00:00Z"},
                        ],
                        "submissions": [
                            {"id": 201, "assignment_id": 101, "score": 8, "grade": "8", "graded_at": "2026-01-10T00:00:00Z"},
                            {"id": 203, "assignment_id": 103, "score": 95, "grade": "95", "graded_at": "2026-01-12T00:00:00Z"},
                        ],
                    }],
                }],
            }, database)

            output = io.StringIO()
            with redirect_stdout(output):
                result = show_alerts(database)

            self.assertEqual(result, 0)
            self.assertIn("Low Score", output.getvalue())
            self.assertIn("80.0%", output.getvalue())
            self.assertIn("Missing Work", output.getvalue())
            self.assertIn("Missing", output.getvalue())
            self.assertNotIn("Good Score", output.getvalue())

    def test_upcoming_query_defaults_to_unsubmitted_work(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "canvas.sqlite3"
            now = datetime.now(timezone.utc)
            due_soon = (now + timedelta(days=2)).isoformat()
            due_soon_submitted = (now + timedelta(days=3)).isoformat()
            due_later = (now + timedelta(days=20)).isoformat()
            store_observed_data({
                "scraped_at": now.isoformat(),
                "source": "Canvas authenticated API",
                "students": [{
                    "student": {"id": 7, "name": "Test Student"},
                    "courses": [{
                        "course": {
                            "id": 11,
                            "name": "Test Course",
                            "enrollments": [{
                                "role": "ObserverEnrollment",
                                "enrollment_state": "active",
                                "current_grading_period_id": 7,
                            }],
                        },
                        "data_status": "available",
                        "grading_periods": [{
                            "id": 7,
                            "title": "Test Quarter",
                            "start_date": "2020-01-01T00:00:00Z",
                            "end_date": "2099-12-31T23:59:59Z",
                        }],
                        "assignments": [
                            {"id": 101, "name": "Due Soon", "points_possible": 10, "due_at": due_soon},
                            {"id": 102, "name": "Already Submitted", "points_possible": 10, "due_at": due_soon_submitted},
                            {"id": 103, "name": "Due Later", "points_possible": 10, "due_at": due_later},
                        ],
                        "submissions": [{
                            "id": 202,
                            "assignment_id": 102,
                            "score": 10,
                            "grade": "10",
                            "submitted_at": now.isoformat(),
                        }],
                    }],
                }],
            }, database)

            output = io.StringIO()
            with redirect_stdout(output):
                result = show_upcoming(database)
            self.assertEqual(result, 0)
            self.assertIn("Due Soon", output.getvalue())
            self.assertNotIn("Already Submitted", output.getvalue())
            self.assertNotIn("Due Later", output.getvalue())

            output = io.StringIO()
            with redirect_stdout(output):
                result = show_upcoming(database, include_submitted=True)
            self.assertEqual(result, 0)
            self.assertIn("Due Soon", output.getvalue())
            self.assertIn("Already Submitted", output.getvalue())

    def test_history_shows_grade_changes_between_syncs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "canvas.sqlite3"
            data = {
                "scraped_at": "2026-01-01T00:00:00+00:00",
                "source": "Canvas authenticated API",
                "students": [{
                    "student": {"id": 7, "name": "Test Student"},
                    "courses": [{
                        "course": {
                            "id": 11,
                            "name": "Test Course",
                            "enrollments": [{"role": "ObserverEnrollment", "enrollment_state": "active"}],
                        },
                        "data_status": "available",
                        "assignments": [{"id": 101, "name": "Quiz", "points_possible": 10}],
                        "submissions": [{
                            "id": 201,
                            "assignment_id": 101,
                            "score": 5,
                            "grade": "5",
                            "graded_at": "2026-01-01T00:00:00Z",
                        }],
                    }],
                }],
            }
            store_observed_data(data, database)
            data["scraped_at"] = "2026-01-02T00:00:00+00:00"
            data["students"][0]["courses"][0]["submissions"][0]["score"] = 8
            data["students"][0]["courses"][0]["submissions"][0]["grade"] = "8"
            store_observed_data(data, database)

            output = io.StringIO()
            with redirect_stdout(output):
                result = show_history(database)

            self.assertEqual(result, 0)
            self.assertIn("Recent syncs", output.getvalue())
            self.assertIn("Recent grade/submission changes", output.getvalue())
            self.assertIn("grade 5/10 → 8/10", output.getvalue())


if __name__ == "__main__":
    unittest.main()
