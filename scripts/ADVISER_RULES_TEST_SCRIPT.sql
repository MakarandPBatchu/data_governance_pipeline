-- =============================================================================
-- ADVISER ASSIGNMENT RULES — Supplemental test script
-- =============================================================================
-- Run AFTER scripts/DQ_TABLES_TEST_SCRIPT.sql
-- Use this to upgrade an existing CLIENTS table or re-apply adviser test scenarios.
--
-- Business rules tested:
--   invalid_im_fp — each client needs at least ONE valid IM or FP (not both required)
--   invalid_rm    — every client needs a valid RM_CODE
-- =============================================================================

SET DB_NAME = 'DQ_TEST_DB';
SET SCHEMA_NAME = 'DQ_TEST_SCHEMA';

USE DATABASE IDENTIFIER($DB_NAME);
USE SCHEMA IDENTIFIER($SCHEMA_NAME);

-- RM reference data
MERGE INTO ADVISERS t
USING (
    SELECT 'ADV006' AS ADVISER_ID, 'RM001' AS ADVISER_CODE, 'Helen Relationship Mgr' AS ADVISER_NAME, 'RM' AS ADVISER_TYPE, TRUE AS IS_ACTIVE, '2018-05-01'::DATE AS CREATED_DATE
    UNION ALL
    SELECT 'ADV007', 'RM002', 'Ian Relationship Mgr', 'RM', TRUE, '2019-03-01'::DATE
) s ON t.ADVISER_ID = s.ADVISER_ID
WHEN NOT MATCHED THEN
    INSERT (ADVISER_ID, ADVISER_CODE, ADVISER_NAME, ADVISER_TYPE, IS_ACTIVE, CREATED_DATE)
    VALUES (s.ADVISER_ID, s.ADVISER_CODE, s.ADVISER_NAME, s.ADVISER_TYPE, s.IS_ACTIVE, s.CREATED_DATE);

ALTER TABLE CLIENTS ADD COLUMN IF NOT EXISTS RM_CODE VARCHAR(20);

-- Valid assignments (should PASS both rules)
UPDATE CLIENTS SET IM_CODE = 'IM001', FP_CODE = 'FP001', RM_CODE = 'RM001' WHERE CLIENT_ID = 'C001';
UPDATE CLIENTS SET IM_CODE = 'IM002', FP_CODE = 'FP002', RM_CODE = 'RM002' WHERE CLIENT_ID = 'C009';
UPDATE CLIENTS SET IM_CODE = 'IM001', FP_CODE = NULL,    RM_CODE = 'RM001' WHERE CLIENT_ID = 'C011';  -- IM only OK
UPDATE CLIENTS SET IM_CODE = NULL,    FP_CODE = 'FP001', RM_CODE = 'RM002' WHERE CLIENT_ID = 'C012';  -- FP only OK
UPDATE CLIENTS SET IM_CODE = 'IM002', FP_CODE = 'FP002', RM_CODE = 'RM001' WHERE CLIENT_ID = 'C015';
UPDATE CLIENTS SET IM_CODE = 'IM002', FP_CODE = 'FP002', RM_CODE = 'RM002' WHERE CLIENT_ID = 'C005';

-- invalid_im_fp failures
UPDATE CLIENTS SET IM_CODE = NULL,    FP_CODE = NULL,    RM_CODE = NULL    WHERE CLIENT_ID = 'C013';  -- missing both
UPDATE CLIENTS SET IM_CODE = 'IM999', FP_CODE = 'FP001', RM_CODE = NULL    WHERE CLIENT_ID = 'C006';  -- invalid IM
UPDATE CLIENTS SET IM_CODE = 'IM001', FP_CODE = 'FP999', RM_CODE = NULL    WHERE CLIENT_ID = 'C007';  -- invalid FP
UPDATE CLIENTS SET IM_CODE = 'IM888', FP_CODE = 'FP888', RM_CODE = NULL    WHERE CLIENT_ID = 'C008';  -- both invalid
UPDATE CLIENTS SET IM_CODE = 'IM777', FP_CODE = 'FP001', RM_CODE = NULL    WHERE CLIENT_ID = 'C010';  -- invalid IM

-- invalid_rm failures (valid IM/FP but RM issue)
UPDATE CLIENTS SET IM_CODE = 'IM001', FP_CODE = 'FP001', RM_CODE = NULL    WHERE CLIENT_ID = 'C004';
UPDATE CLIENTS SET IM_CODE = 'IM003', FP_CODE = 'FP002', RM_CODE = 'RM999' WHERE CLIENT_ID = 'C014';
UPDATE CLIENTS SET IM_CODE = 'IM001', FP_CODE = 'FP001', RM_CODE = NULL    WHERE CLIENT_ID = 'C002';
UPDATE CLIENTS SET IM_CODE = 'IM002', FP_CODE = 'FP002', RM_CODE = NULL    WHERE CLIENT_ID = 'C003';

-- =============================================================================
-- VERIFICATION
-- =============================================================================

-- invalid_im_fp — expect 5 rows
SELECT c.CLIENT_ID, c.IM_CODE, c.FP_CODE, 'invalid_im_fp' AS ISSUE_TYPE
FROM CLIENTS c
LEFT JOIN ADVISERS im ON c.IM_CODE = im.ADVISER_CODE
LEFT JOIN ADVISERS fp ON c.FP_CODE = fp.ADVISER_CODE
WHERE COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND (
      (c.IM_CODE IS NULL AND c.FP_CODE IS NULL)
      OR (c.IM_CODE IS NOT NULL AND im.ADVISER_ID IS NULL)
      OR (c.FP_CODE IS NOT NULL AND fp.ADVISER_ID IS NULL)
  )
ORDER BY c.CLIENT_ID;

-- invalid_rm — expect 9 rows
SELECT c.CLIENT_ID, c.RM_CODE, 'invalid_rm' AS ISSUE_TYPE
FROM CLIENTS c
LEFT JOIN ADVISERS rm ON c.RM_CODE = rm.ADVISER_CODE
WHERE COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND (c.RM_CODE IS NULL OR rm.ADVISER_ID IS NULL)
ORDER BY c.CLIENT_ID;

-- IM-only / FP-only clients that should PASS invalid_im_fp (expect 0 rows)
SELECT c.CLIENT_ID, c.IM_CODE, c.FP_CODE
FROM CLIENTS c
LEFT JOIN ADVISERS im ON c.IM_CODE = im.ADVISER_CODE
LEFT JOIN ADVISERS fp ON c.FP_CODE = fp.ADVISER_CODE
WHERE c.CLIENT_ID IN ('C011', 'C012')
  AND (
      (c.IM_CODE IS NULL AND c.FP_CODE IS NULL)
      OR (c.IM_CODE IS NOT NULL AND im.ADVISER_ID IS NULL)
      OR (c.FP_CODE IS NOT NULL AND fp.ADVISER_ID IS NULL)
  );
