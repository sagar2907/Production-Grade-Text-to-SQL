-- Rosetta: a deliberately hard schema, modelled on the Indian Union Budget.
--
-- The point of this schema is NOT that it is large. It is that it is
-- ambiguous in the specific ways real government financial data is ambiguous,
-- so that a question like "what was health spending in 2019?" has more than one
-- defensible answer and a model that picks one silently is wrong in a way
-- nobody notices.
--
-- The engineered ambiguities, each modelled on a real property of the data:
--
--   1. BE / RE / ACTUAL          "the budget for X" means one of three numbers.
--                                Budget Estimate, Revised Estimate, and what
--                                was actually spent. They differ, often a lot.
--
--   2. UNITS                     Amounts appear in crore, lakh and rupees in
--                                different columns. Summing the wrong one is
--                                off by 100x, runs cleanly, and returns a
--                                plausible number. This is the single best
--                                confidently-wrong generator in the schema.
--
--   3. GROSS vs NET              Net is gross minus recoveries. Both are
--                                "expenditure".
--
--   4. PLAN / NON-PLAN           Abolished from FY2017-18 and replaced by
--                                Scheme / Establishment. Pre-2017 rows have
--                                the first pair populated and the second NULL;
--                                post-2017 rows have the reverse. A query that
--                                sums plan_amount across all years silently
--                                drops seven years of data.
--
--   5. MINISTRY RENAMES          Ministries are renamed, merged and split.
--                                "Ministry of Education" does not exist before
--                                2020; the same money sits under "Ministry of
--                                Human Resource Development".
--
--   6. TWO JOIN PATHS            allocations -> ministry directly, or
--                                allocations -> demand -> ministry. They
--                                disagree wherever a demand was reassigned
--                                between ministries mid-period. Neither is
--                                wrong; only one is certified.
--
-- Every one of these is documented in semantic/ as either a certified metric,
-- a glossary entry, or a validated join path. The schema is the problem; the
-- semantic layer is the answer; the eval measures the gap between them.

DROP TABLE IF EXISTS transfers_to_states;
DROP TABLE IF EXISTS receipts;
DROP TABLE IF EXISTS expenditure_actuals;
DROP TABLE IF EXISTS budget_allocations;
DROP TABLE IF EXISTS schemes;
DROP TABLE IF EXISTS account_heads;
DROP TABLE IF EXISTS demands;
DROP TABLE IF EXISTS departments;
DROP TABLE IF EXISTS ministries;
DROP TABLE IF EXISTS states;
DROP TABLE IF EXISTS fiscal_years;


-- Reference: fiscal years, and which accounting regime was in force.
CREATE TABLE fiscal_years (
    fiscal_year          VARCHAR PRIMARY KEY,   -- '2019-20'
    start_year           INTEGER NOT NULL,
    end_year             INTEGER NOT NULL,
    label_short          VARCHAR,               -- 'FY20'
    is_plan_regime       BOOLEAN NOT NULL,      -- FALSE from 2017-18 onward
    classification_note  VARCHAR,
    actuals_available    BOOLEAN NOT NULL,      -- the latest years have no actuals yet
    re_available         BOOLEAN NOT NULL,
    presented_on         DATE,
    is_interim_budget    BOOLEAN,
    notes                VARCHAR
);


CREATE TABLE states (
    state_code           VARCHAR PRIMARY KEY,
    state_name           VARCHAR NOT NULL,
    state_name_short     VARCHAR,
    region               VARCHAR,
    is_union_territory   BOOLEAN NOT NULL,
    is_special_category  BOOLEAN,
    population_census    BIGINT,
    formed_year          INTEGER,
    successor_state_code VARCHAR,               -- for states split mid-period
    notes                VARCHAR
);


-- Ministries are versioned by year range, because they get renamed.
CREATE TABLE ministries (
    ministry_id          INTEGER PRIMARY KEY,
    ministry_name        VARCHAR NOT NULL,
    ministry_name_short  VARCHAR,
    ministry_code        VARCHAR,
    sector               VARCHAR,
    -- 'ministry' | 'department' | 'functional'. Functional entries (Interest
    -- Payments, Defence Pensions, Transfers to States) are real spending lines
    -- in the published budget but are NOT administrative ministries. Total
    -- expenditure includes them; "what ministries spent" does not. Which one a
    -- question means is a certified decision, not a guess.
    entity_kind          VARCHAR,
    effective_from_year  INTEGER NOT NULL,
    effective_to_year    INTEGER,               -- NULL = still current
    renamed_from_id      INTEGER,               -- the same money, older name
    merged_into_id       INTEGER,
    is_active            BOOLEAN NOT NULL,
    department_count     INTEGER,
    notes                VARCHAR
);


CREATE TABLE departments (
    department_id        INTEGER PRIMARY KEY,
    department_name      VARCHAR NOT NULL,
    department_code      VARCHAR,
    ministry_id          INTEGER NOT NULL,
    effective_from_year  INTEGER,
    effective_to_year    INTEGER,
    is_active            BOOLEAN,
    head_of_dept         VARCHAR,
    notes                VARCHAR
);


-- Demands for Grants. The demand number is NOT stable across years.
CREATE TABLE demands (
    demand_id            INTEGER PRIMARY KEY,
    demand_no            INTEGER NOT NULL,
    demand_name          VARCHAR NOT NULL,
    fiscal_year          VARCHAR NOT NULL,
    ministry_id          INTEGER NOT NULL,
    department_id        INTEGER,
    demand_type          VARCHAR,               -- 'revenue' | 'capital' | 'combined'
    is_charged           BOOLEAN,               -- charged on Consolidated Fund
    voted_amount_crore   DOUBLE,
    charged_amount_crore DOUBLE,
    total_be_crore       DOUBLE,
    total_re_crore       DOUBLE,
    total_actual_crore   DOUBLE,
    notes                VARCHAR
);


-- The account head hierarchy: Major -> Sub-Major -> Minor -> Sub -> Object.
CREATE TABLE account_heads (
    head_code            VARCHAR PRIMARY KEY,
    major_head           VARCHAR NOT NULL,
    major_head_desc      VARCHAR,
    sub_major_head       VARCHAR,
    minor_head           VARCHAR,
    minor_head_desc      VARCHAR,
    sub_head             VARCHAR,
    object_head          VARCHAR,
    head_description     VARCHAR,
    account_type         VARCHAR,               -- 'revenue' | 'capital' | 'loan'
    is_revenue           BOOLEAN,
    is_capital           BOOLEAN,
    is_charged           BOOLEAN,
    parent_code          VARCHAR,
    hierarchy_level      INTEGER,
    effective_from_year  INTEGER,
    effective_to_year    INTEGER,
    sort_order           INTEGER
);


CREATE TABLE schemes (
    scheme_id            INTEGER PRIMARY KEY,
    scheme_name          VARCHAR NOT NULL,
    scheme_name_alt      VARCHAR,               -- the other name people use
    scheme_code          VARCHAR,
    ministry_id          INTEGER,
    scheme_type          VARCHAR,               -- 'CSS' | 'CS' | 'establishment'
    is_centrally_sponsored BOOLEAN,
    is_central_sector    BOOLEAN,
    umbrella_scheme_id   INTEGER,               -- sub-schemes roll up
    merged_into_id       INTEGER,               -- schemes get merged
    launch_year          INTEGER,
    closure_year         INTEGER,
    funding_pattern_centre INTEGER,             -- e.g. 60 (percent)
    funding_pattern_state  INTEGER,             -- e.g. 40
    sector               VARCHAR,
    sub_sector           VARCHAR,
    status               VARCHAR,
    description          VARCHAR
);


-- The wide fact table. Every amount ambiguity lives here.
CREATE TABLE budget_allocations (
    allocation_id        BIGINT PRIMARY KEY,
    fiscal_year          VARCHAR NOT NULL,
    demand_id            INTEGER,               -- join path A: via demand
    ministry_id          INTEGER,               -- join path B: direct
    department_id        INTEGER,
    scheme_id            INTEGER,
    head_code            VARCHAR,
    state_code           VARCHAR,

    -- (1) the three estimates. "the budget" is one of these three.
    be_amount_crore      DOUBLE,                -- Budget Estimate
    re_amount_crore      DOUBLE,                -- Revised Estimate
    actual_amount_crore  DOUBLE,                -- what was actually spent

    -- (2) the same figure in three units. Summing the wrong one is 100x out.
    be_amount_lakh       DOUBLE,
    be_amount_rupees     DOUBLE,

    -- (3) gross and net differ by recoveries.
    gross_amount_crore   DOUBLE,
    net_amount_crore     DOUBLE,
    recoveries_crore     DOUBLE,

    -- revenue / capital split
    revenue_amount_crore DOUBLE,
    capital_amount_crore DOUBLE,
    loan_amount_crore    DOUBLE,

    -- (4) the 2017 discontinuity: exactly one of these pairs is populated.
    plan_amount_crore     DOUBLE,               -- NULL from 2017-18
    non_plan_amount_crore DOUBLE,               -- NULL from 2017-18
    scheme_amount_crore   DOUBLE,               -- NULL before 2017-18
    establishment_amount_crore DOUBLE,          -- NULL before 2017-18

    is_charged           BOOLEAN,
    is_voted             BOOLEAN,
    is_transfer_to_state BOOLEAN,
    is_supplementary     BOOLEAN,               -- supplementary demands
    supplementary_batch  VARCHAR,
    revision_no          INTEGER,
    source_document      VARCHAR,
    entered_on           DATE,
    last_revised_on      DATE,
    notes                VARCHAR
);


-- A SECOND fact table at a different grain: monthly, not annual.
-- Joining this to budget_allocations and summing without aggregating first
-- multiplies rows. This is the classic fan-trap and it is here on purpose.
CREATE TABLE expenditure_actuals (
    actual_id            BIGINT PRIMARY KEY,
    fiscal_year          VARCHAR NOT NULL,
    month_no             INTEGER NOT NULL,      -- 1 = April
    month_name           VARCHAR,
    ministry_id          INTEGER,
    demand_id            INTEGER,
    scheme_id            INTEGER,
    head_code            VARCHAR,
    state_code           VARCHAR,
    expenditure_crore    DOUBLE,
    expenditure_lakh     DOUBLE,
    revenue_exp_crore    DOUBLE,
    capital_exp_crore    DOUBLE,
    recoveries_crore     DOUBLE,
    is_provisional       BOOLEAN,
    is_revised           BOOLEAN,
    reported_on          DATE,
    reporting_lag_days   INTEGER,
    source_system        VARCHAR,
    notes                VARCHAR
);


CREATE TABLE receipts (
    receipt_id           BIGINT PRIMARY KEY,
    fiscal_year          VARCHAR NOT NULL,
    receipt_type         VARCHAR,               -- 'tax' | 'non_tax' | 'capital'
    receipt_category     VARCHAR,
    receipt_head         VARCHAR,
    head_code            VARCHAR,
    be_amount_crore      DOUBLE,
    re_amount_crore      DOUBLE,
    actual_amount_crore  DOUBLE,
    gross_amount_crore   DOUBLE,
    net_amount_crore     DOUBLE,
    -- Whether borrowings count as a "receipt" is the classic trap: total
    -- receipts including borrowings is a different number from revenue
    -- receipts, and both get called "government income".
    is_borrowing         BOOLEAN,
    is_revenue_receipt   BOOLEAN,
    is_capital_receipt   BOOLEAN,
    devolution_to_states_crore DOUBLE,
    net_to_centre_crore  DOUBLE,
    collection_cost_crore DOUBLE,
    notes                VARCHAR
);


CREATE TABLE transfers_to_states (
    transfer_id          BIGINT PRIMARY KEY,
    fiscal_year          VARCHAR NOT NULL,
    state_code           VARCHAR NOT NULL,
    ministry_id          INTEGER,
    scheme_id            INTEGER,
    transfer_type        VARCHAR,               -- 'devolution'|'grant'|'loan'
    finance_commission   VARCHAR,
    be_amount_crore      DOUBLE,
    re_amount_crore      DOUBLE,
    actual_amount_crore  DOUBLE,
    centre_share_crore   DOUBLE,
    state_share_crore    DOUBLE,
    is_tied_grant        BOOLEAN,
    is_untied            BOOLEAN,
    released_on          DATE,
    utilisation_pct      DOUBLE,
    pending_uc_crore     DOUBLE,                -- pending utilisation certificates
    notes                VARCHAR
);
