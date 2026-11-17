import sqlite3
import tempfile
import unittest
from pathlib import Path

from canvas_scraper import store_observed_data


class SQLiteStorageTests(unittest.TestCase):
    def test_reruns_keep_separate_submission_snapshots(self):
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
                            "course_code": "TEST-1",
                            "enrollments": [{
                                "role": "ObserverEnrollment",
                                "computed_current_score": 87.5,
                            }],
                        },
                        "data_status": "available",
                        "assignments": [{
                            "id": 101,
                            "name": "Test Assignment",
                            "points_possible": 10,
                        }],
                        "submissions": [{
                            "id": 201,
                            "assignment_id": 101,
                            "score": 8,
                            "grade": "8",
                        }],
                    }],
                }],
            }

            first_run = store_observed_data(data, database)
            data["scraped_at"] = "2026-01-02T00:00:00+00:00"
            data["students"][0]["courses"][0]["submissions"][0]["score"] = 9
            second_run = store_observed_data(data, database)

            with sqlite3.connect(database) as connection:
                runs = connection.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0]
                submissions = connection.execute(
                    "SELECT scrape_run_id, score FROM submissions ORDER BY scrape_run_id"
                ).fetchall()
                assignments = connection.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
                enrollment_score = connection.execute("SELECT current_score FROM enrollments").fetchone()[0]

            self.assertEqual((first_run, second_run), (1, 2))
            self.assertEqual(runs, 2)
            self.assertEqual(submissions, [(1, 8.0), (2, 9.0)])
            self.assertEqual(assignments, 2)
            self.assertEqual(enrollment_score, 87.5)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
