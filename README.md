# Canvas Watch

A small local CLI that signs in to Canvas in a browser and exports data visible
to the signed-in account. By default it finds observed students and collects
their accessible courses, assignment details, submissions, grades, and available
submission comments/rubrics into SQLite. It is intended for accounts and records
you are authorized to access.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Edit `.env` and set `CANVAS_USERNAME`, `CANVAS_PASSWORD`, and `CANVAS_URL` for
your own Canvas instance. The `.env` file and
the default `data/` output directory are ignored by Git. Never commit your
credentials or exported grades.

If you use [direnv](https://direnv.net/), the included `.envrc` loads `.env` and
adds `bin/` to your `PATH` when you enter this directory. Approve it with
`direnv allow` (and ensure your shell has the direnv hook enabled) to use the
`canvas` command.

## Canvas command

```bash
canvas sync
```

`sync` fetches all observed students and their accessible course/grade data into
`data/canvas.sqlite3`. Each run is recorded separately so later runs preserve
grade changes. `mirror` is an alias for `sync`. Filter to particular students
if needed:

```bash
canvas sync --students '<student name>'
```

The database stores students, courses, enrollments/grade totals, assignments,
submissions, and a per-course status for each scrape run. Raw Canvas objects are
retained in JSON columns. Course or submission data Canvas does not make
available to the signed-in account is marked as restricted or partial. The
database is created with owner-only permissions and is ignored by Git.

## List students and current classes

Show each student and courses whose term dates include today:

```bash
canvas list
canvas list classes
canvas list students
```

Filter to one student or check a different date:

```bash
canvas list classes --student '<student name>'
canvas list --as-of 2026-11-16
```

Courses with active enrollments but no term dates are shown separately rather
than assumed to be current.

Show assignments, scores, due dates, and submission status for a student in a
class. By default, this is limited to the current Canvas grading period (usually
the quarter). The course name can be a partial match:

```bash
canvas show --student '<student name>' --course '<course name>'
canvas show --student '<student name>' --course '<course name>' --all-periods
```

Find assignments with no submission, score, grade, or grading timestamp across
all students' current quarters:

```bash
canvas missing
canvas missing --student '<student name>'
```

Add `--all-periods` to include assignments from past and undated periods. The reusable SQLite
query is in `queries/not_submitted.sql`. Results distinguish overdue from
not-yet-due assignments. Canvas-restricted course data is noted and cannot be
included if the account could not access it.

Find assignments below 90% or marked missing in the current quarter:

```bash
canvas alerts
canvas alerts --student '<student name>'
canvas alerts --threshold 85
canvas alerts --all-periods
```

The SQL is in `queries/below_threshold_or_missing.sql`. Percentages use Canvas
score divided by points possible; letter-only grades without a numeric score
are not comparable to a percentage threshold.

Show unsubmitted assignments due in the next seven days in the current quarter:

```bash
canvas upcoming
canvas upcoming --days 14
canvas upcoming --student '<student name>'
canvas upcoming --include-submitted
```

Use `--all-periods` to look beyond the current quarter. The query is in
`queries/upcoming.sql`.

Review recent syncs and grade/submission changes:

```bash
canvas history
canvas history --limit 20
canvas history --student '<student name>'
```

Download Canvas-hosted files linked from assignment descriptions in the current
quarter. `attachments` is an alias for `materials`:

```bash
canvas materials --dry-run
canvas materials
canvas attachments --student '<student name>'
canvas materials --all-periods
```

Files and a manifest are stored under the Git-ignored `data/materials/`
directory. Only files hosted by this Canvas instance and linked from matching
assignments are downloaded; arbitrary external links and student-submitted work
are not included. Use `--refresh` to download files again or `--limit` to adjust
the maximum number of unique files processed.

Use `--json` to also save a timestamped JSON snapshot, or `--output PATH` to
choose a snapshot path:

```bash
canvas sync --json
```

For a direct SQL query of assignments and grades in each student's current
grading period:

```sql
WITH latest AS (
  SELECT student_id, course_id, MAX(scrape_run_id) AS run_id
  FROM course_scrapes GROUP BY student_id, course_id
)
SELECT s.name AS student, c.name AS course, a.name AS assignment,
       a.due_at, sub.score, sub.grade, gp.title AS grading_period
FROM latest AS l
JOIN course_scrapes AS cs
  ON cs.student_id = l.student_id AND cs.course_id = l.course_id
 AND cs.scrape_run_id = l.run_id
JOIN students AS s ON s.id = cs.student_id
JOIN courses AS c ON c.id = cs.course_id
JOIN enrollments AS e
  ON e.scrape_run_id = cs.scrape_run_id
 AND e.student_id = cs.student_id AND e.course_id = cs.course_id
JOIN grading_periods AS gp
  ON gp.scrape_run_id = e.scrape_run_id
 AND gp.student_id = e.student_id AND gp.course_id = e.course_id
 AND gp.grading_period_id = json_extract(e.raw_json, '$.current_grading_period_id')
JOIN assignments AS a
  ON a.scrape_run_id = cs.scrape_run_id
 AND a.student_id = cs.student_id AND a.course_id = cs.course_id
LEFT JOIN submissions AS sub
  ON sub.scrape_run_id = a.scrape_run_id
 AND sub.student_id = a.student_id AND sub.course_id = a.course_id
 AND sub.assignment_id = a.assignment_id
WHERE e.enrollment_state = 'active'
  AND datetime(a.due_at) BETWEEN datetime(gp.start_at) AND datetime(gp.end_at)
ORDER BY student, course, assignment;
```

## Scrape one page

To capture visible text, tables, and links from one page instead, use:

```bash
canvas scrape
canvas scrape 'https://your-school.instructure.com/courses/COURSE_ID/grades/USER_ID'
```

By default Chromium runs headlessly. Use `--headed` if your account requires
MFA, SSO, or an interactive sign-in step; complete it in the browser window.
Use `--output path/to/file.json` to choose an output file. Otherwise, a
timestamped JSON file is written under `data/`.

Canvas may change its sign-in flow or page markup. This starter handles the
standard Canvas username/password form and captures the rendered page; it does
not bypass MFA or other access controls. Keep the output private because grade
and course information can be sensitive.

Run `canvas help` for the command list; use `canvas sync --help`,
`canvas scrape --help`, or `canvas list --help` for command options.
