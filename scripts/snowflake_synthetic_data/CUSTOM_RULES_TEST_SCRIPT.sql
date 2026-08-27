-- =============================================================================
-- CUSTOM RULES — Supplemental test script
-- =============================================================================
-- Run AFTER scripts/snowflake_synthetic_data/DQ_TABLES_TEST_SCRIPT.sql
-- (ADVISER_RULES_TEST_SCRIPT.sql is optional; this script does not depend on it.)
--
-- Purpose: seed rows that fire the two rules in config/custom_rules.csv, without
-- UPDATE/DELETE of existing CLIENTS / ADVISERS / other tables used for YAML
-- business rules and profiler checks.
--
-- Custom rules tested:
--   inactive_im_assigned        — non-deleted client whose IM_CODE is an inactive adviser
--   zero_or_negative_aum_active — ACTIVE, not deleted, AUM is NULL, 0, or negative
--
-- This script only INSERT/MERGEs:
--   ADVISERS.ADV101  (new inactive IM code IM101)
--   CLIENTS.CR001–CR004, CR011–CR017
-- =============================================================================

SET DB_NAME = 'DQ_TEST_DB';
SET SCHEMA_NAME = 'DQ_TEST_SCHEMA';

USE DATABASE IDENTIFIER($DB_NAME);
USE SCHEMA IDENTIFIER($SCHEMA_NAME);

-- -----------------------------------------------------------------------------
-- 1. New inactive IM (do not change ADV005 / IM003 — C014 already uses it)
-- -----------------------------------------------------------------------------
MERGE INTO ADVISERS t
USING (
    SELECT
        'ADV101' AS ADVISER_ID,
        'IM101' AS ADVISER_CODE,
        'Frank Investment Mgr' AS ADVISER_NAME,
        'IM' AS ADVISER_TYPE,
        FALSE AS IS_ACTIVE,
        '2022-01-01'::DATE AS CREATED_DATE
) s
ON t.ADVISER_ID = s.ADVISER_ID
WHEN NOT MATCHED THEN
    INSERT (ADVISER_ID, ADVISER_CODE, ADVISER_NAME, ADVISER_TYPE, IS_ACTIVE, CREATED_DATE)
    VALUES (s.ADVISER_ID, s.ADVISER_CODE, s.ADVISER_NAME, s.ADVISER_TYPE, s.IS_ACTIVE, s.CREATED_DATE)
WHEN MATCHED THEN
    UPDATE SET
        ADVISER_CODE = s.ADVISER_CODE,
        ADVISER_NAME = s.ADVISER_NAME,
        ADVISER_TYPE = s.ADVISER_TYPE,
        IS_ACTIVE = s.IS_ACTIVE,
        CREATED_DATE = s.CREATED_DATE;

-- -----------------------------------------------------------------------------
-- 2. Custom-rule fixture clients (unique IDs; created 2022+; valid FP/RM)
-- -----------------------------------------------------------------------------
-- CR001  Oliver Bennett     HIT  inactive_im_assigned (IM101 inactive, not deleted)
-- CR003  Priya Sharma       skip deleted + inactive IM
-- CR004  Thomas Walker      skip active IM (IM001)
-- CR011  Emma Collins       HIT  zero/negative AUM (AUM = 0)
-- CR012  Daniel Hughes      HIT  zero/negative AUM (AUM negative)
-- CR013  Sophia Martinez    HIT  zero/negative AUM (AUM NULL)
-- CR014  William Chen       skip positive AUM
-- CR015  Amelia Brooks      skip ARCHIVED with AUM 0 (status is not ACTIVE)
-- CR016  Noah Patel         skip SUSPENDED with negative AUM
-- CR017  Isabella Rossi     skip deleted with AUM 0
--
-- Existing seed hit (not modified here):
-- C014   HIT  inactive_im_assigned (IM003 is already IS_ACTIVE = FALSE)
-- -----------------------------------------------------------------------------
MERGE INTO CLIENTS t
USING (
    SELECT 'CR001' AS CLIENT_ID, 'Oliver Bennett' AS CLIENT_NAME, 'ACTIVE' AS CLIENT_STATUS,
           125000.00 AS AUM, '2022-04-01'::DATE AS CREATED_DATE, FALSE AS IS_DELETED,
           'IM101' AS IM_CODE, 'FP001' AS FP_CODE, 'RM001' AS RM_CODE
    UNION ALL
    SELECT 'CR003', 'Priya Sharma', 'ACTIVE',
           80000.00, '2022-04-01'::DATE, TRUE,
           'IM101', 'FP001', 'RM001'
    UNION ALL
    SELECT 'CR004', 'Thomas Walker', 'ACTIVE',
           90000.00, '2022-04-01'::DATE, FALSE,
           'IM001', 'FP001', 'RM001'
    UNION ALL
    SELECT 'CR011', 'Emma Collins', 'ACTIVE',
           0.00, '2022-05-01'::DATE, FALSE,
           'IM001', 'FP001', 'RM001'
    UNION ALL
    SELECT 'CR012', 'Daniel Hughes', 'ACTIVE',
           -2500.00, '2022-05-01'::DATE, FALSE,
           'IM002', 'FP002', 'RM002'
    UNION ALL
    SELECT 'CR013', 'Sophia Martinez', 'ACTIVE',
           NULL, '2022-05-01'::DATE, FALSE,
           'IM001', 'FP002', 'RM001'
    UNION ALL
    SELECT 'CR014', 'William Chen', 'ACTIVE',
           50000.00, '2022-05-01'::DATE, FALSE,
           'IM001', 'FP001', 'RM002'
    UNION ALL
    SELECT 'CR015', 'Amelia Brooks', 'ARCHIVED',
           0.00, '2022-06-01'::DATE, FALSE,
           'IM001', 'FP001', 'RM001'
    UNION ALL
    SELECT 'CR016', 'Noah Patel', 'SUSPENDED',
           -10.00, '2022-06-01'::DATE, FALSE,
           'IM002', 'FP001', 'RM002'
    UNION ALL
    SELECT 'CR017', 'Isabella Rossi', 'ACTIVE',
           0.00, '2022-06-01'::DATE, TRUE,
           'IM001', 'FP001', 'RM001'
) s
ON t.CLIENT_ID = s.CLIENT_ID
WHEN NOT MATCHED THEN
    INSERT (CLIENT_ID, CLIENT_NAME, CLIENT_STATUS, AUM, CREATED_DATE, IS_DELETED, IM_CODE, FP_CODE, RM_CODE)
    VALUES (s.CLIENT_ID, s.CLIENT_NAME, s.CLIENT_STATUS, s.AUM, s.CREATED_DATE, s.IS_DELETED, s.IM_CODE, s.FP_CODE, s.RM_CODE)
WHEN MATCHED THEN
    UPDATE SET
        CLIENT_NAME = s.CLIENT_NAME,
        CLIENT_STATUS = s.CLIENT_STATUS,
        AUM = s.AUM,
        CREATED_DATE = s.CREATED_DATE,
        IS_DELETED = s.IS_DELETED,
        IM_CODE = s.IM_CODE,
        FP_CODE = s.FP_CODE,
        RM_CODE = s.RM_CODE;

-- =============================================================================
-- VERIFICATION — custom rules (same intent as config/custom_rules.csv)
-- =============================================================================

-- inactive_im_assigned — expect 2 rows: C014 (seed), CR001 (new)
-- C014 uses existing inactive IM003; CR001 uses new inactive IM101
-- CR003 is deleted so it must not appear
SELECT
    c.CLIENT_ID,
    c.CLIENT_NAME,
    c.IM_CODE,
    a.ADVISER_NAME,
    a.IS_ACTIVE,
    'inactive_im_assigned' AS ISSUE_TYPE,
    'Client IM_CODE ' || c.IM_CODE || ' is assigned to inactive adviser ' || a.ADVISER_NAME AS ISSUE_DETAIL
FROM CLIENTS c
INNER JOIN ADVISERS a ON c.IM_CODE = a.ADVISER_CODE
WHERE COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND a.IS_ACTIVE = FALSE
ORDER BY c.CLIENT_ID;

-- Confirm expected ids only
SELECT CLIENT_ID
FROM CLIENTS c
INNER JOIN ADVISERS a ON c.IM_CODE = a.ADVISER_CODE
WHERE COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND a.IS_ACTIVE = FALSE
ORDER BY c.CLIENT_ID;
-- Expected: C014, CR001

-- Negative: deleted + inactive IM must be 0 rows
SELECT c.CLIENT_ID
FROM CLIENTS c
INNER JOIN ADVISERS a ON c.IM_CODE = a.ADVISER_CODE
WHERE c.CLIENT_ID = 'CR003'
  AND COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND a.IS_ACTIVE = FALSE;
-- Expected: 0 rows

-- zero_or_negative_aum_active — expect 3 rows: CR011, CR012, CR013
SELECT
    c.CLIENT_ID,
    c.CLIENT_NAME,
    c.CLIENT_STATUS,
    c.AUM,
    'zero_or_negative_aum_active' AS ISSUE_TYPE,
    CASE
        WHEN c.AUM IS NULL THEN 'Active client has NULL AUM'
        WHEN c.AUM = 0 THEN 'Active client has zero AUM'
        ELSE 'Active client has negative AUM = ' || CAST(c.AUM AS VARCHAR)
    END AS ISSUE_DETAIL
FROM CLIENTS c
WHERE UPPER(c.CLIENT_STATUS) = 'ACTIVE'
  AND COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND (c.AUM IS NULL OR c.AUM <= 0)
ORDER BY c.CLIENT_ID;
-- Expected: CR011 (0), CR012 (negative), CR013 (NULL)

-- Negative controls must be 0 rows (archived / suspended / deleted / positive AUM)
SELECT CLIENT_ID, CLIENT_STATUS, AUM, IS_DELETED
FROM CLIENTS
WHERE CLIENT_ID IN ('CR014', 'CR015', 'CR016', 'CR017')
  AND UPPER(CLIENT_STATUS) = 'ACTIVE'
  AND COALESCE(IS_DELETED, FALSE) = FALSE
  AND (AUM IS NULL OR AUM <= 0);
-- Expected: 0 rows

-- =============================================================================
-- SAFETY CHECK — original YAML / profiler fixtures must be unchanged
-- =============================================================================
-- These counts must match DQ_TABLES_TEST_SCRIPT.sql comments after this script.

-- Rule 1 archived + AUM — expect 3 rows (C002, C003, C010)
SELECT COUNT(*) AS archived_with_aum_count
FROM CLIENTS
WHERE UPPER(CLIENT_STATUS) = 'ARCHIVED' AND COALESCE(AUM, 0) > 0;

-- Rule 2 pre-2017 not deleted — expect 3 rows (C004, C005, C010)
SELECT COUNT(*) AS pre_2017_not_deleted_count
FROM CLIENTS
WHERE CREATED_DATE < '2017-01-01' AND COALESCE(IS_DELETED, FALSE) = FALSE;

-- Rule 3 invalid IM/FP — expect 5 rows (C006, C007, C008, C010, C013)
SELECT COUNT(*) AS invalid_im_fp_count
FROM CLIENTS c
LEFT JOIN ADVISERS im ON c.IM_CODE = im.ADVISER_CODE
LEFT JOIN ADVISERS fp ON c.FP_CODE = fp.ADVISER_CODE
WHERE COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND (
      (c.IM_CODE IS NULL AND c.FP_CODE IS NULL)
      OR (c.IM_CODE IS NOT NULL AND im.ADVISER_ID IS NULL)
      OR (c.FP_CODE IS NOT NULL AND fp.ADVISER_ID IS NULL)
  );

-- Rule 4 invalid RM — expect 9 rows
SELECT COUNT(*) AS invalid_rm_count
FROM CLIENTS c
LEFT JOIN ADVISERS rm ON c.RM_CODE = rm.ADVISER_CODE
WHERE COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND (c.RM_CODE IS NULL OR rm.ADVISER_ID IS NULL);

-- Duplicate PKs — unchanged (this script does not touch these tables)
SELECT TRANSACTION_ID, COUNT(*) AS dup_count FROM TRANSACTIONS GROUP BY 1 HAVING COUNT(*) > 1;
SELECT PORTFOLIO_ID, COUNT(*) AS dup_count FROM PORTFOLIOS GROUP BY 1 HAVING COUNT(*) > 1;

-- =============================================================================
-- OPTIONAL CLEANUP — custom-rule fixtures only (leave commented unless needed)
-- =============================================================================
-- DELETE FROM CLIENTS WHERE CLIENT_ID IN (
--     'CR001', 'CR003', 'CR004',
--     'CR011', 'CR012', 'CR013', 'CR014', 'CR015', 'CR016', 'CR017'
-- );
-- DELETE FROM ADVISERS WHERE ADVISER_ID = 'ADV101';
