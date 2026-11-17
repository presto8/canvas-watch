WITH latest_course_runs AS (
    SELECT student_id, course_id, MAX(scrape_run_id) AS scrape_run_id
    FROM course_scrapes
    GROUP BY student_id, course_id
)
SELECT
    s.name AS student,
    COALESCE(c.name, 'Course ' || cs.course_id) AS course,
    json_extract(c.raw_json, '$.time_zone') AS time_zone,
    COALESCE(a.name, 'Assignment ' || a.assignment_id) AS assignment,
    a.due_at,
    a.points_possible,
    cs.data_status,
    CASE
        WHEN COALESCE(trim(sub.submitted_at), '') <> '' THEN 'Submitted'
        WHEN sub.score IS NOT NULL OR sub.grade IS NOT NULL OR sub.graded_at IS NOT NULL THEN 'Graded'
        ELSE 'Not submitted'
    END AS status
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
WHERE e.enrollment_state = 'active'
  AND COALESCE(json_extract(a.raw_json, '$.published'), 1) <> 0
  AND COALESCE(json_extract(sub.raw_json, '$.excused'), 0) = 0
  AND datetime(a.due_at) >= datetime('now')
  AND datetime(a.due_at) <= datetime('now', '+' || CAST(:days AS TEXT) || ' days')
  AND (
      :all_periods = 1
      OR (
          gp.grading_period_id IS NOT NULL
          AND (
              datetime(a.due_at) BETWEEN datetime(gp.start_at) AND datetime(gp.end_at)
              OR CAST(json_extract(sub.raw_json, '$.grading_period_id') AS INTEGER) = gp.grading_period_id
              OR CAST(json_extract(a.raw_json, '$.grading_period_id') AS INTEGER) = gp.grading_period_id
          )
      )
  )
  AND (
      :include_submitted = 1
      OR (
          COALESCE(trim(sub.submitted_at), '') = ''
          AND sub.score IS NULL
          AND sub.grade IS NULL
          AND sub.graded_at IS NULL
          AND COALESCE(lower(sub.workflow_state), 'unsubmitted') NOT IN ('submitted', 'graded', 'pending_review')
      )
  )
  AND (:student IS NULL OR lower(s.name) LIKE '%' || lower(:student) || '%')
ORDER BY a.due_at, s.name, c.name, a.name;
