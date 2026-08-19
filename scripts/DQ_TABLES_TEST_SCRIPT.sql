-- =============================================================================
-- Data Governance Pipeline — Synthetic Test Schema (10 tables)
-- Replace database/schema names, then run in Snowflake worksheet.
-- =============================================================================

SET DB_NAME = 'DQ_TEST_DB';
SET SCHEMA_NAME = 'DQ_TEST_SCHEMA';

CREATE DATABASE IF NOT EXISTS IDENTIFIER($DB_NAME);
--CREATE SCHEMA IF NOT EXISTS IDENTIFIER($DB_NAME).IDENTIFIER($SCHEMA_NAME);
USE DATABASE IDENTIFIER($DB_NAME);
USE SCHEMA IDENTIFIER($SCHEMA_NAME);

-- Drop existing objects (idempotent re-run)
DROP TABLE IF EXISTS FEE_SCHEDULES;
DROP TABLE IF EXISTS PRODUCTS;
DROP TABLE IF EXISTS CLIENT_DOCUMENTS;
DROP TABLE IF EXISTS COMPLAINTS;
DROP TABLE IF EXISTS CLIENT_HOLDINGS;
DROP TABLE IF EXISTS CLIENT_CONTACTS;
DROP TABLE IF EXISTS PORTFOLIOS;
DROP TABLE IF EXISTS TRANSACTIONS;
DROP TABLE IF EXISTS CLIENTS;
DROP TABLE IF EXISTS ADVISERS;

-- =============================================================================
-- 1. ADVISERS (reference — valid IM / FP / RM codes)
-- =============================================================================
CREATE TABLE ADVISERS (
    ADVISER_ID     VARCHAR(20)  NOT NULL,
    ADVISER_CODE   VARCHAR(20)  NOT NULL,
    ADVISER_NAME   VARCHAR(100),
    ADVISER_TYPE   VARCHAR(20),   -- IM / FP / RM
    IS_ACTIVE      BOOLEAN       DEFAULT TRUE,
    CREATED_DATE   DATE
);

INSERT INTO ADVISERS VALUES
('ADV001', 'IM001', 'Alice Investment Mgr',   'IM', TRUE,  '2018-03-01'),
('ADV002', 'FP001', 'Bob Financial Planner',  'FP', TRUE,  '2018-04-15'),
('ADV003', 'IM002', 'Carol Investment Mgr',   'IM', TRUE,  '2019-01-10'),
('ADV004', 'FP002', 'Dan Financial Planner',  'FP', TRUE,  '2019-06-20'),
('ADV005', 'IM003', 'Eve Investment Mgr',     'IM', FALSE, '2020-02-01'),
('ADV006', 'RM001', 'Helen Relationship Mgr', 'RM', TRUE,  '2018-05-01'),
('ADV007', 'RM002', 'Ian Relationship Mgr',   'RM', TRUE,  '2019-03-01');

-- =============================================================================
-- 2. CLIENTS (business rules: archived+AUM, pre-2017, IM/FP, RM)
-- =============================================================================
CREATE TABLE CLIENTS (
    CLIENT_ID      VARCHAR(20)  NOT NULL,
    CLIENT_NAME    VARCHAR(100),
    CLIENT_STATUS  VARCHAR(20),   -- ACTIVE / ARCHIVED / SUSPENDED
    AUM            NUMBER(18,2),
    CREATED_DATE   DATE,
    IS_DELETED     BOOLEAN       DEFAULT FALSE,
    IM_CODE        VARCHAR(20),
    FP_CODE        VARCHAR(20),
    RM_CODE        VARCHAR(20),
    UPDATED_AT     TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

INSERT INTO CLIENTS (CLIENT_ID, CLIENT_NAME, CLIENT_STATUS, AUM, CREATED_DATE, IS_DELETED, IM_CODE, FP_CODE, RM_CODE) VALUES
-- Clean rows (valid IM+FP+RM, or valid IM-only / FP-only + RM)
('C001', 'Clean Active Client',    'ACTIVE',   250000.00, '2020-01-15', FALSE, 'IM001', 'FP001', 'RM001'),
('C009', 'Another Clean Client',   'ACTIVE',   180000.00, '2021-06-01', FALSE, 'IM002', 'FP002', 'RM002'),
('C011', 'IM Only Client',         'ACTIVE',    95000.00, '2022-03-10', FALSE, 'IM001', NULL,    'RM001'),  -- IM only OK
('C012', 'FP Only Client',         'ACTIVE',   120000.00, '2023-08-22', FALSE, NULL,    'FP001', 'RM002'),  -- FP only OK
('C015', 'Baseline Client 15',     'ACTIVE',   300000.00, '2025-02-14', FALSE, 'IM002', 'FP002', 'RM001'),
('C005', 'Legacy Client 2016',     'ACTIVE',    32000.00, '2016-12-01', FALSE, 'IM002', 'FP002', 'RM002'),

-- Rule 1: archived_client_with_aum (valid IM/FP, missing RM flagged separately)
('C002', 'Archived With AUM 1',    'ARCHIVED', 150000.00, '2019-05-01', FALSE, 'IM001', 'FP001', NULL),
('C003', 'Archived With AUM 2',    'ARCHIVED',  82000.50, '2018-11-20', FALSE, 'IM002', 'FP002', NULL),

-- Rule 2: pre_2017_client_not_deleted
('C004', 'Legacy Client 2015',     'ACTIVE',    45000.00, '2015-08-12', FALSE, 'IM001', 'FP001', NULL),   -- missing RM

-- Rule 3: invalid_im_fp — missing both advisers
('C013', 'No IM Or FP Client',       'SUSPENDED', 50000.00, '2024-01-05', FALSE, NULL,    NULL,    NULL),

-- Rule 3: invalid_im_fp — invalid IM or FP codes
('C006', 'Invalid IM Code',        'ACTIVE',   110000.00, '2020-07-01', FALSE, 'IM999', 'FP001', NULL),
('C007', 'Invalid FP Code',        'ACTIVE',    88000.00, '2021-02-15', FALSE, 'IM001', 'FP999', NULL),
('C008', 'Invalid IM And FP',      'ACTIVE',    67000.00, '2021-09-30', FALSE, 'IM888', 'FP888', NULL),

-- Rule 4: invalid_rm — bad RM code (valid IM/FP)
('C014', 'Invalid RM Code',        'ACTIVE',    75000.00, '2024-11-01', FALSE, 'IM003', 'FP002', 'RM999'),

-- Edge case: multiple violations
('C010', 'Multi-Issue Client',     'ARCHIVED', 200000.00, '2014-03-15', FALSE, 'IM777', 'FP001', NULL);

-- =============================================================================
-- 3. TRANSACTIONS (duplicate PK — no UNIQUE constraint on purpose)
-- =============================================================================
CREATE TABLE TRANSACTIONS (
    TRANSACTION_ID   VARCHAR(20)  NOT NULL,
    CLIENT_ID        VARCHAR(20),
    TRANSACTION_DATE DATE,
    AMOUNT           NUMBER(18,2),
    TRANSACTION_TYPE VARCHAR(30)
);

INSERT INTO TRANSACTIONS VALUES
('TXN001', 'C001', '2024-01-10',  5000.00, 'DEPOSIT'),
('TXN002', 'C001', '2024-02-15', -1200.00, 'WITHDRAWAL'),
('TXN003', 'C009', '2024-03-01',  8000.00, 'DEPOSIT'),
('TXN004', 'C011', '2024-04-20',  2500.00, 'DIVIDEND'),
('TXN005', 'C012', '2024-05-05', -3000.00, 'WITHDRAWAL'),
-- Duplicate PK group 1 (same TRANSACTION_ID twice)
('TXN_DUP1', 'C002', '2024-06-01', 1000.00, 'DEPOSIT'),
('TXN_DUP1', 'C002', '2024-06-02', 1500.00, 'DEPOSIT'),   -- duplicate
-- Duplicate PK group 2
('TXN_DUP2', 'C006', '2024-07-01',  500.00, 'FEE'),
('TXN_DUP2', 'C006', '2024-07-01',  500.00, 'FEE');       -- duplicate

-- =============================================================================
-- 4. PORTFOLIOS (duplicate PK)
-- =============================================================================
CREATE TABLE PORTFOLIOS (
    PORTFOLIO_ID   VARCHAR(20)  NOT NULL,
    CLIENT_ID      VARCHAR(20),
    PORTFOLIO_NAME VARCHAR(100),
    TOTAL_VALUE    NUMBER(18,2)
);

INSERT INTO PORTFOLIOS VALUES
('PF001', 'C001', 'Growth Portfolio',   250000.00),
('PF002', 'C009', 'Income Portfolio',   180000.00),
('PF003', 'C011', 'Balanced Portfolio',  95000.00),
-- Duplicate PK group 1
('PF_DUP1', 'C002', 'Legacy Portfolio A', 150000.00),
('PF_DUP1', 'C002', 'Legacy Portfolio B', 150000.00),  -- duplicate ID, different name
-- Duplicate PK group 2
('PF_DUP2', 'C004', 'Old Portfolio',      45000.00),
('PF_DUP2', 'C004', 'Old Portfolio Copy',  45000.00);

-- =============================================================================
-- 5. CLIENT_CONTACTS (high null rate: 8/20 = 40% null on EMAIL and PHONE)
-- =============================================================================
CREATE TABLE CLIENT_CONTACTS (
    CONTACT_ID   VARCHAR(20)  NOT NULL,
    CLIENT_ID    VARCHAR(20),
    CONTACT_TYPE VARCHAR(20),
    EMAIL        VARCHAR(100),
    PHONE        VARCHAR(30),
    IS_PRIMARY   BOOLEAN
);

INSERT INTO CLIENT_CONTACTS (CONTACT_ID, CLIENT_ID, CONTACT_TYPE, EMAIL, PHONE, IS_PRIMARY) VALUES
('CT001', 'C001', 'PRIMARY',   'client001@email.com', '+44-7700-900001', TRUE),
('CT002', 'C009', 'PRIMARY',   'client009@email.com', '+44-7700-900009', TRUE),
('CT003', 'C011', 'PRIMARY',   'client011@email.com', '+44-7700-900011', TRUE),
('CT004', 'C012', 'PRIMARY',   'client012@email.com', '+44-7700-900012', TRUE),
('CT005', 'C013', 'PRIMARY',   'client013@email.com', '+44-7700-900013', TRUE),
('CT006', 'C014', 'PRIMARY',   'client014@email.com', '+44-7700-900014', TRUE),
('CT007', 'C015', 'PRIMARY',   'client015@email.com', '+44-7700-900015', TRUE),
('CT008', 'C002', 'PRIMARY',   'archived002@email.com', '+44-7700-900002', TRUE),
('CT009', 'C004', 'SECONDARY', 'legacy004@email.com',   '+44-7700-900004', FALSE),
('CT010', 'C006', 'PRIMARY',   'invalid006@email.com',  '+44-7700-900006', TRUE),
('CT011', 'C001', 'SECONDARY', NULL, NULL, FALSE),  -- null pair 1
('CT012', 'C009', 'SECONDARY', NULL, NULL, FALSE),  -- null pair 2
('CT013', 'C011', 'SECONDARY', NULL, NULL, FALSE),  -- null pair 3
('CT014', 'C012', 'SECONDARY', NULL, NULL, FALSE),  -- null pair 4
('CT015', 'C013', 'SECONDARY', NULL, NULL, FALSE),  -- null pair 5
('CT016', 'C014', 'SECONDARY', NULL, NULL, FALSE),  -- null pair 6
('CT017', 'C015', 'SECONDARY', NULL, NULL, FALSE),  -- null pair 7
('CT018', 'C002', 'SECONDARY', NULL, NULL, FALSE),  -- null pair 8
('CT019', 'C004', 'SECONDARY', 'partial004@email.com', NULL, FALSE),
('CT020', 'C006', 'SECONDARY', NULL, '+44-7700-900006', FALSE);

-- =============================================================================
-- 6. CLIENT_HOLDINGS (high null on MARKET_VALUE: 7/20 = 35%)
-- =============================================================================
CREATE TABLE CLIENT_HOLDINGS (
    HOLDING_ID     VARCHAR(20)  NOT NULL,
    CLIENT_ID      VARCHAR(20),
    PRODUCT_CODE   VARCHAR(20),
    UNITS          NUMBER(18,4),
    MARKET_VALUE   NUMBER(18,2),
    AS_AT_DATE     DATE
);

INSERT INTO CLIENT_HOLDINGS (HOLDING_ID, CLIENT_ID, PRODUCT_CODE, UNITS, MARKET_VALUE, AS_AT_DATE) VALUES
('H001', 'C001', 'EQ-UK',  1000.0000,  25000.00, '2025-01-31'),
('H002', 'C001', 'BD-UK',  5000.0000,  52000.00, '2025-01-31'),
('H003', 'C009', 'EQ-US',   800.0000,  48000.00, '2025-01-31'),
('H004', 'C011', 'EQ-UK',   400.0000,  10000.00, '2025-01-31'),
('H005', 'C012', 'MF-GLB', 2000.0000,  35000.00, '2025-01-31'),
('H006', 'C013', 'EQ-UK',   300.0000,   7500.00, '2025-01-31'),
('H007', 'C014', 'BD-EU',  1500.0000,  15000.00, '2025-01-31'),
('H008', 'C015', 'EQ-UK',  2000.0000,  60000.00, '2025-01-31'),
('H009', 'C002', 'EQ-UK',  3000.0000,  75000.00, '2025-01-31'),  -- archived client holding
('H010', 'C004', 'BD-UK',   500.0000,   5000.00, '2025-01-31'),
('H011', 'C006', 'EQ-US',   600.0000,  36000.00, '2025-01-31'),
('H012', 'C001', 'ALT-01',  100.0000,  NULL, '2025-01-31'),  -- null MV 1
('H013', 'C009', 'ALT-02',  100.0000,  NULL, '2025-01-31'),
('H014', 'C011', 'ALT-03',  100.0000,  NULL, '2025-01-31'),
('H015', 'C012', 'ALT-04',  100.0000,  NULL, '2025-01-31'),
('H016', 'C013', 'ALT-05',  100.0000,  NULL, '2025-01-31'),
('H017', 'C014', 'ALT-06',  100.0000,  NULL, '2025-01-31'),
('H018', 'C015', 'ALT-07',  100.0000,  NULL, '2025-01-31');

-- =============================================================================
-- 7. COMPLAINTS (high null on RESOLUTION_DATE: 6/15 = 40%)
-- =============================================================================
CREATE TABLE COMPLAINTS (
    COMPLAINT_ID      VARCHAR(20)  NOT NULL,
    CLIENT_ID         VARCHAR(20),
    COMPLAINT_DATE    DATE,
    RESOLUTION_DATE   DATE,
    STATUS            VARCHAR(20),
    DESCRIPTION       VARCHAR(500)
);

INSERT INTO COMPLAINTS (COMPLAINT_ID, CLIENT_ID, COMPLAINT_DATE, RESOLUTION_DATE, STATUS, DESCRIPTION) VALUES
('CMP001', 'C001', '2024-01-15', '2024-02-01', 'CLOSED',   'Fee query resolved'),
('CMP002', 'C009', '2024-03-10', '2024-03-25', 'CLOSED',   'Statement delay'),
('CMP003', 'C011', '2024-04-05', '2024-04-20', 'CLOSED',   'Transfer issue'),
('CMP004', 'C012', '2024-05-12', '2024-06-01', 'CLOSED',   'Adviser change request'),
('CMP005', 'C013', '2024-06-01', NULL,         'OPEN',     'Unresolved service complaint'),      -- null 1
('CMP006', 'C014', '2024-07-01', NULL,         'OPEN',     'Pending review'),                    -- null 2
('CMP007', 'C015', '2024-08-01', NULL,         'OPEN',     'Awaiting client response'),           -- null 3
('CMP008', 'C002', '2024-09-01', NULL,         'OPEN',     'Archived client complaint open'),    -- null 4
('CMP009', 'C004', '2024-10-01', NULL,         'OPEN',     'Legacy client complaint'),           -- null 5
('CMP010', 'C006', '2024-11-01', NULL,         'OPEN',     'Invalid IM linked complaint'),       -- null 6
('CMP011', 'C007', '2024-12-01', '2025-01-10', 'CLOSED',   'Closed complaint'),
('CMP012', 'C008', '2025-01-05', '2025-01-20', 'CLOSED',   'Closed complaint'),
('CMP013', 'C010', '2025-01-10', NULL,         'OPEN',     'Multi-issue client complaint'),
('CMP014', 'C003', '2025-01-15', NULL,         'OPEN',     'Archived client open case'),
('CMP015', 'C005', '2025-01-20', '2025-02-01', 'CLOSED',   'Legacy client resolved');

-- =============================================================================
-- 8. CLIENT_DOCUMENTS (high null on DOCUMENT_TYPE: 5/15 = 33%)
-- =============================================================================
CREATE TABLE CLIENT_DOCUMENTS (
    DOCUMENT_ID    VARCHAR(20)  NOT NULL,
    CLIENT_ID      VARCHAR(20),
    DOCUMENT_TYPE  VARCHAR(50),
    UPLOAD_DATE    DATE,
    FILE_NAME      VARCHAR(200)
);

INSERT INTO CLIENT_DOCUMENTS (DOCUMENT_ID, CLIENT_ID, DOCUMENT_TYPE, UPLOAD_DATE, FILE_NAME) VALUES
('DOC001', 'C001', 'KYC',           '2020-01-20', 'kyc_c001.pdf'),
('DOC002', 'C009', 'KYC',           '2021-06-05', 'kyc_c009.pdf'),
('DOC003', 'C011', 'SUITABILITY',   '2022-03-15', 'suit_c011.pdf'),
('DOC004', 'C012', 'KYC',           '2023-08-25', 'kyc_c012.pdf'),
('DOC005', 'C013', 'CONTRACT',      '2024-01-10', 'contract_c013.pdf'),
('DOC006', 'C014', 'KYC',           '2024-11-05', 'kyc_c014.pdf'),
('DOC007', 'C015', 'SUITABILITY',   '2025-02-20', 'suit_c015.pdf'),
('DOC008', 'C002', 'KYC',           '2019-05-10', 'kyc_c002.pdf'),
('DOC009', 'C004', 'KYC',           '2015-08-20', 'kyc_c004.pdf'),
('DOC010', 'C006', NULL,            '2020-07-05', 'unknown_doc.pdf'),   -- null type 1
('DOC011', 'C007', NULL,            '2021-02-20', 'scan001.pdf'),        -- null type 2
('DOC012', 'C008', NULL,            '2021-10-01', 'scan002.pdf'),        -- null type 3
('DOC013', 'C010', NULL,            '2014-03-20', 'legacy_doc.pdf'),     -- null type 4
('DOC014', 'C003', NULL,            '2018-11-25', 'archived_doc.pdf'),   -- null type 5
('DOC015', 'C005', 'KYC',           '2016-12-10', 'kyc_c005.pdf');

-- =============================================================================
-- 9. PRODUCTS (clean control table — should pass profiling)
-- =============================================================================
CREATE TABLE PRODUCTS (
    PRODUCT_CODE   VARCHAR(20)  NOT NULL,
    PRODUCT_NAME   VARCHAR(100) NOT NULL,
    ASSET_CLASS    VARCHAR(30)  NOT NULL,
    IS_ACTIVE      BOOLEAN       DEFAULT TRUE,
    LAUNCH_DATE    DATE
);

INSERT INTO PRODUCTS VALUES
('EQ-UK',  'UK Equity Fund',       'EQUITY',    TRUE,  '2010-01-01'),
('EQ-US',  'US Equity Fund',       'EQUITY',    TRUE,  '2010-06-01'),
('BD-UK',  'UK Bond Fund',         'FIXED_INC', TRUE,  '2011-01-01'),
('BD-EU',  'EU Bond Fund',         'FIXED_INC', TRUE,  '2012-03-01'),
('MF-GLB', 'Global Multi-Asset',   'MIXED',     TRUE,  '2015-09-01'),
('ALT-01', 'Alternative Asset 1',  'ALTERNATIVE', TRUE, '2018-01-01'),
('ALT-02', 'Alternative Asset 2',  'ALTERNATIVE', TRUE, '2018-02-01'),
('ALT-03', 'Alternative Asset 3',  'ALTERNATIVE', TRUE, '2018-03-01'),
('ALT-04', 'Alternative Asset 4',  'ALTERNATIVE', TRUE, '2018-04-01'),
('ALT-05', 'Alternative Asset 5',  'ALTERNATIVE', TRUE, '2018-05-01'),
('ALT-06', 'Alternative Asset 6',  'ALTERNATIVE', TRUE, '2018-06-01'),
('ALT-07', 'Alternative Asset 7',  'ALTERNATIVE', TRUE, '2018-07-01');

-- =============================================================================
-- 10. FEE_SCHEDULES (high null on FEE_RATE: 4/12 = 33%)
-- =============================================================================
CREATE TABLE FEE_SCHEDULES (
    FEE_SCHEDULE_ID  VARCHAR(20)  NOT NULL,
    CLIENT_ID        VARCHAR(20),
    FEE_TYPE         VARCHAR(30),
    FEE_RATE         NUMBER(8,4),
    EFFECTIVE_FROM   DATE
);

INSERT INTO FEE_SCHEDULES (FEE_SCHEDULE_ID, CLIENT_ID, FEE_TYPE, FEE_RATE, EFFECTIVE_FROM) VALUES
('FS001', 'C001', 'ADVISER',    0.0075, '2020-01-15'),
('FS002', 'C009', 'ADVISER',    0.0100, '2021-06-01'),
('FS003', 'C011', 'ADVISER',    0.0080, '2022-03-10'),
('FS004', 'C012', 'ADVISER',    0.0090, '2023-08-22'),
('FS005', 'C013', 'ADVISER',    0.0075, '2024-01-05'),
('FS006', 'C014', 'ADVISER',    0.0085, '2024-11-01'),
('FS007', 'C015', 'ADVISER',    0.0100, '2025-02-14'),
('FS008', 'C002', 'ADVISER',    NULL,   '2019-05-01'),  -- null rate 1 (archived)
('FS009', 'C004', 'ADVISER',    NULL,   '2015-08-12'),  -- null rate 2 (pre-2017)
('FS010', 'C006', 'ADVISER',    NULL,   '2020-07-01'),  -- null rate 3 (bad IM)
('FS011', 'C010', 'ADVISER',    NULL,   '2014-03-15'),  -- null rate 4 (multi-issue)
('FS012', 'C001', 'PLATFORM',   0.0025, '2020-01-15');

-- =============================================================================
-- Verification queries (run before pipeline)
-- =============================================================================

-- Rule 1: archived + AUM (expect 3 rows)
SELECT CLIENT_ID, CLIENT_STATUS, AUM
FROM CLIENTS
WHERE UPPER(CLIENT_STATUS) = 'ARCHIVED' AND COALESCE(AUM, 0) > 0;

-- Rule 2: pre-2017 not deleted (expect 3 rows)
SELECT CLIENT_ID, CREATED_DATE, IS_DELETED
FROM CLIENTS
WHERE CREATED_DATE < '2017-01-01' AND COALESCE(IS_DELETED, FALSE) = FALSE;

-- Rule 3: invalid IM/FP — missing or invalid adviser (expect 5 rows: C013, C006, C007, C008, C010)
SELECT
    c.CLIENT_ID,
    c.IM_CODE,
    c.FP_CODE,
    CASE
        WHEN c.IM_CODE IS NULL AND c.FP_CODE IS NULL
            THEN 'Missing IM and FP — at least one adviser required'
        WHEN c.IM_CODE IS NOT NULL AND im.ADVISER_ID IS NULL
            THEN 'Invalid IM code: ' || c.IM_CODE
        WHEN c.FP_CODE IS NOT NULL AND fp.ADVISER_ID IS NULL
            THEN 'Invalid FP code: ' || c.FP_CODE
    END AS ISSUE_DETAIL
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

-- Rule 4: invalid RM — missing or invalid RM (expect 9 rows)
SELECT
    c.CLIENT_ID,
    c.RM_CODE,
    CASE
        WHEN c.RM_CODE IS NULL THEN 'Missing RM code — Relationship Manager required'
        ELSE 'Invalid RM code: ' || c.RM_CODE
    END AS ISSUE_DETAIL
FROM CLIENTS c
LEFT JOIN ADVISERS rm ON c.RM_CODE = rm.ADVISER_CODE
WHERE COALESCE(c.IS_DELETED, FALSE) = FALSE
  AND (c.RM_CODE IS NULL OR rm.ADVISER_ID IS NULL)
ORDER BY c.CLIENT_ID;

-- Duplicate PKs
SELECT TRANSACTION_ID, COUNT(*) FROM TRANSACTIONS GROUP BY 1 HAVING COUNT(*) > 1;
SELECT PORTFOLIO_ID, COUNT(*) FROM PORTFOLIOS GROUP BY 1 HAVING COUNT(*) > 1;

-- High null rates (>5%)
SELECT 'CLIENT_CONTACTS.EMAIL' AS col,
       COUNT_IF(EMAIL IS NULL) / COUNT(*)::FLOAT AS null_rate FROM CLIENT_CONTACTS
UNION ALL
SELECT 'CLIENT_HOLDINGS.MARKET_VALUE', COUNT_IF(MARKET_VALUE IS NULL) / COUNT(*)::FLOAT FROM CLIENT_HOLDINGS
UNION ALL
SELECT 'COMPLAINTS.RESOLUTION_DATE', COUNT_IF(RESOLUTION_DATE IS NULL) / COUNT(*)::FLOAT FROM COMPLAINTS
UNION ALL
SELECT 'CLIENT_DOCUMENTS.DOCUMENT_TYPE', COUNT_IF(DOCUMENT_TYPE IS NULL) / COUNT(*)::FLOAT FROM CLIENT_DOCUMENTS
UNION ALL
SELECT 'FEE_SCHEDULES.FEE_RATE', COUNT_IF(FEE_RATE IS NULL) / COUNT(*)::FLOAT FROM FEE_SCHEDULES;