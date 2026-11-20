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
      OR EXISTS (
          SELECT 1 FROM grading_periods AS target_gp
          WHERE target_gp.scrape_run_id = cs.scrape_run_id
            AND target_gp.student_id = cs.student_id
            AND target_gp.course_id = cs.course_id
            AND (
                (:academic_year IS NULL AND target_gp.grading_period_id = CAST(json_extract(e.raw_json, '$.current_grading_period_id') AS INTEGER))
                OR (
                    :academic_year IS NOT NULL
                    AND CASE
                        WHEN json_extract(cs.raw_course_json, '$.term.name') GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9][0-9][0-9]*'
                            THEN CAST(substr(json_extract(cs.raw_course_json, '$.term.name'), 1, 4) AS INTEGER)
                        ELSE CAST(strftime('%Y', COALESCE(json_extract(cs.raw_course_json, '$.term.start_at'), json_extract(cs.raw_course_json, '$.start_at'))) AS INTEGER)
                             - CASE WHEN CAST(strftime('%m', COALESCE(json_extract(cs.raw_course_json, '$.term.start_at'), json_extract(cs.raw_course_json, '$.start_at'))) AS INTEGER) < 7 THEN 1 ELSE 0 END
                    END = :academic_year
                    AND (
                        (:quarter_number = 1 AND lower(target_gp.title) IN ('quarter 1', 'q1'))
                        OR (:quarter_number = 2 AND lower(target_gp.title) IN ('quarter 2', 'q2'))
                        OR (:quarter_number = 3 AND lower(target_gp.title) IN ('quarter 3', 'q3'))
                        OR (:quarter_number = 4 AND lower(target_gp.title) IN ('quarter 4', 'q4'))
                        OR (:semester_number = 1 AND lower(target_gp.title) IN ('quarter 1', 'quarter 2', 'q1', 'q2'))
                        OR (:semester_number = 2 AND lower(target_gp.title) IN ('quarter 3', 'quarter 4', 'q3', 'q4'))
                    )
                )
            )
            AND (
                datetime(a.due_at) BETWEEN datetime(target_gp.start_at) AND datetime(target_gp.end_at)
                OR CAST(json_extract(sub.raw_json, '$.grading_period_id') AS INTEGER) = target_gp.grading_period_id
                OR CAST(json_extract(a.raw_json, '$.grading_period_id') AS INTEGER) = target_gp.grading_period_id
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
