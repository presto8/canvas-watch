WITH latest_course_runs AS (
    SELECT student_id, course_id, MAX(scrape_run_id) AS scrape_run_id
    FROM course_scrapes
    GROUP BY student_id, course_id
), records AS (
    SELECT
        s.name AS student,
        COALESCE(c.name, 'Course ' || cs.course_id) AS course,
        COALESCE(a.name, 'Assignment ' || a.assignment_id) AS assignment,
        a.assignment_id,
        a.due_at,
        a.points_possible,
        a.raw_json AS assignment_json,
        sub.score,
        sub.grade,
        sub.submitted_at,
        sub.graded_at,
        sub.workflow_state,
        sub.raw_json AS submission_json,
        cs.data_status,
        CAST(json_extract(a.raw_json, '$.grading_type') AS TEXT) AS grading_type,
        CASE
            WHEN sub.score IS NOT NULL AND a.points_possible > 0
                THEN 100.0 * sub.score / a.points_possible
            WHEN instr(COALESCE(sub.grade, ''), '%') > 0
                THEN CAST(replace(sub.grade, '%', '') AS REAL)
            WHEN sub.score IS NULL
                 AND trim(COALESCE(sub.grade, '')) <> ''
                 AND trim(sub.grade) NOT GLOB '*[^0-9.]*'
                 AND a.points_possible > 0
                THEN 100.0 * CAST(sub.grade AS REAL) / a.points_possible
            WHEN sub.score IS NULL
                 AND trim(COALESCE(sub.grade, '')) <> ''
                 AND trim(sub.grade) NOT GLOB '*[^0-9.]*'
                 AND CAST(json_extract(a.raw_json, '$.grading_type') AS TEXT) = 'percent'
                THEN CAST(sub.grade AS REAL)
            ELSE NULL
        END AS percentage
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
      AND (:student IS NULL OR lower(s.name) LIKE '%' || lower(:student) || '%')
      AND (
          :all_periods = 1
          OR EXISTS (
              SELECT 1 FROM grading_periods AS target_gp
              WHERE target_gp.scrape_run_id = cs.scrape_run_id
                AND target_gp.student_id = cs.student_id
                AND target_gp.course_id = cs.course_id
                AND (
                    (:academic_year IS NULL AND :grade_level IS NULL AND target_gp.grading_period_id = CAST(json_extract(e.raw_json, '$.current_grading_period_id') AS INTEGER))
                    OR (
                        (:academic_year IS NOT NULL OR :grade_level IS NOT NULL)
                        AND CASE
                            WHEN json_extract(cs.raw_course_json, '$.term.name') GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9][0-9][0-9]*'
                                THEN CAST(substr(json_extract(cs.raw_course_json, '$.term.name'), 1, 4) AS INTEGER)
                            ELSE CAST(strftime('%Y', COALESCE(json_extract(cs.raw_course_json, '$.term.start_at'), json_extract(cs.raw_course_json, '$.start_at'))) AS INTEGER)
                                 - CASE WHEN CAST(strftime('%m', COALESCE(json_extract(cs.raw_course_json, '$.term.start_at'), json_extract(cs.raw_course_json, '$.start_at'))) AS INTEGER) < 7 THEN 1 ELSE 0 END
                        END = COALESCE(:academic_year, s.graduation_year + :grade_level - 13)
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
                    (a.due_at IS NOT NULL AND datetime(a.due_at) BETWEEN datetime(target_gp.start_at) AND datetime(target_gp.end_at))
                    OR CAST(json_extract(sub.raw_json, '$.grading_period_id') AS INTEGER) = target_gp.grading_period_id
                    OR CAST(json_extract(a.raw_json, '$.grading_period_id') AS INTEGER) = target_gp.grading_period_id
                )
          )
      )
)
SELECT
    student,
    course,
    assignment,
    due_at,
    score,
    points_possible,
    grade,
    percentage,
    data_status,
    CASE
        WHEN COALESCE(json_extract(submission_json, '$.missing'), 0) = 1 THEN 'Missing'
        WHEN COALESCE(trim(submitted_at), '') = ''
             AND score IS NULL
             AND grade IS NULL
             AND graded_at IS NULL
             AND datetime(due_at) < datetime('now')
             AND COALESCE(lower(workflow_state), 'unsubmitted') NOT IN ('submitted', 'graded', 'pending_review')
            THEN 'Missing'
        ELSE 'Below threshold'
    END AS reason
FROM records
WHERE (
    (percentage IS NOT NULL AND percentage < :threshold)
    OR COALESCE(json_extract(submission_json, '$.missing'), 0) = 1
    OR (
        COALESCE(trim(submitted_at), '') = ''
        AND score IS NULL
        AND grade IS NULL
        AND graded_at IS NULL
        AND due_at IS NOT NULL
        AND datetime(due_at) < datetime('now')
        AND COALESCE(lower(workflow_state), 'unsubmitted') NOT IN ('submitted', 'graded', 'pending_review')
    )
)
ORDER BY student, course, CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, assignment;
