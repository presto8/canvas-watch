WITH latest_course_runs AS (
    SELECT student_id, course_id, MAX(scrape_run_id) AS scrape_run_id
    FROM course_scrapes
    GROUP BY student_id, course_id
)
SELECT
    s.name AS student,
    COALESCE(c.name, 'Course ' || cs.course_id) AS course,
    COALESCE(a.name, 'Assignment ' || a.assignment_id) AS assignment,
    a.due_at,
    CASE
        WHEN a.due_at IS NULL THEN 'No due date'
        WHEN date(a.due_at) < date('now') THEN 'Overdue'
        ELSE 'Not yet due'
    END AS due_status,
    cs.data_status,
    gp.title AS current_period
FROM latest_course_runs AS latest
JOIN course_scrapes AS cs
  ON cs.scrape_run_id = latest.scrape_run_id
 AND cs.student_id = latest.student_id
 AND cs.course_id = latest.course_id
JOIN students AS s ON s.id = cs.student_id
LEFT JOIN courses AS c ON c.id = cs.course_id
JOIN enrollments AS e
  ON e.scrape_run_id = cs.scrape_run_id
 AND e.student_id = cs.student_id
 AND e.course_id = cs.course_id
LEFT JOIN grading_periods AS gp
  ON gp.scrape_run_id = e.scrape_run_id
 AND gp.student_id = e.student_id
 AND gp.course_id = e.course_id
 AND gp.grading_period_id = CAST(json_extract(e.raw_json, '$.current_grading_period_id') AS INTEGER)
JOIN assignments AS a
  ON a.scrape_run_id = cs.scrape_run_id
 AND a.student_id = cs.student_id
 AND a.course_id = cs.course_id
LEFT JOIN submissions AS sub
  ON sub.scrape_run_id = a.scrape_run_id
 AND sub.student_id = a.student_id
 AND sub.course_id = a.course_id
 AND sub.assignment_id = a.assignment_id
WHERE COALESCE(json_extract(a.raw_json, '$.published'), 1) <> 0
  AND e.enrollment_state = 'active'
  AND (
      :all_periods = 1
      OR (
          gp.grading_period_id IS NOT NULL
          AND a.due_at IS NOT NULL
          AND datetime(a.due_at) BETWEEN datetime(gp.start_at) AND datetime(gp.end_at)
      )
  )
  AND COALESCE(trim(sub.submitted_at), '') = ''
  AND sub.score IS NULL
  AND sub.grade IS NULL
  AND sub.graded_at IS NULL
  AND COALESCE(lower(sub.workflow_state), 'unsubmitted') NOT IN ('submitted', 'graded', 'pending_review')
  AND COALESCE(json_extract(sub.raw_json, '$.excused'), 0) = 0
  AND (:student IS NULL OR lower(s.name) LIKE '%' || lower(:student) || '%')
ORDER BY CASE WHEN a.due_at IS NULL THEN 1 ELSE 0 END, a.due_at, s.name, c.name, a.name;
