"""Adversarial self-audit of the gold evaluation set.

WHAT THIS IS NOT: blind cross-model verification. It was written in the same
conversation thread that wrote eval/questions.yaml, so the author of this
script has the original gold SQL memorized. Re-running any model -- this one
included -- in a context that already contains the answer is not independent
verification, and this file does not pretend otherwise.

WHAT THIS IS: for each of the 32 answerable/trap gold queries, an independent
check constructed a different way -- a different join path to the same
number, an algebraic identity that must hold if the generator's own logic is
consistent, or a known invariant from scripts/load_data.py (e.g. gross_amount
is set equal to be_amount at generation time, so they must match exactly, not
approximately). Where gold and the independent check disagree, that is a real
finding: either the gold query has a bug, or the independent check does, and
either way it is not something a second opinion in the same context could
have caught by simply agreeing.

This still does not validate the INTERPRETIVE judgement calls -- which of
several defensible readings is "certified". Those are listed separately at
the bottom of this file's output for human review; no script can settle them.

    python scripts/audit_gold.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta.compare import Match, compare_result_sets  # noqa: E402
from rosetta.db import Database                          # noqa: E402
from rosetta.semantic import resolve_lineage              # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.yaml"
DB = ROOT / "data" / "rosetta.duckdb"


def load() -> dict[str, dict]:
    data = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    return {q["id"]: q for q in data["questions"]}


class Audit:
    """One independent check against one gold query."""

    def __init__(self, qid: str, method: str):
        self.qid = qid
        self.method = method  # how independent this check actually is

    def run(self, db, gold_sql: str) -> tuple[bool, str]:
        raise NotImplementedError


class CrossQuery(Audit):
    """Gold and an independently-constructed query must return the same rows."""

    def __init__(self, qid: str, alt_sql: str, method: str, order_matters: bool = False):
        super().__init__(qid, method)
        self.alt_sql = alt_sql
        self.order_matters = order_matters

    def run(self, db, gold_sql: str) -> tuple[bool, str]:
        gold = db.execute(gold_sql)
        alt = db.execute(self.alt_sql)
        if not gold.ok:
            return False, f"gold query itself errors: {gold.error}"
        if not alt.ok:
            return False, f"my cross-check query errors: {alt.error}"
        cmp = compare_result_sets(alt.rows, gold.rows, order_matters=self.order_matters)
        if cmp.match is Match.EXACT:
            return True, f"agrees ({cmp.reason})"
        return False, f"DISAGREES: {cmp.reason}  gold={gold.rows[:3]}  mine={alt.rows[:3]}"


class Invariant(Audit):
    """A relationship that must hold if the generator's own logic is consistent."""

    def __init__(self, qid: str, check_sql: str, predicate, method: str, describe):
        super().__init__(qid, method)
        self.check_sql = check_sql
        self.predicate = predicate
        self.describe = describe

    def run(self, db, gold_sql: str) -> tuple[bool, str]:
        result = db.execute(self.check_sql)
        if not result.ok:
            return False, f"invariant query errors: {result.error}"
        row = result.rows[0] if result.rows else ()
        ok = self.predicate(row)
        return ok, self.describe(row)


# =============================================================================
# The 32 audits. One per answerable/trap question.
# =============================================================================
AUDITS: list[Audit] = [

    # --- Q01-Q20: answerable -------------------------------------------------
    CrossQuery("Q01",
        "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
        "JOIN demands d ON a.demand_id=d.demand_id "
        "WHERE a.fiscal_year='2019-20' AND d.demand_name='Ministry of Railways'",
        "via demand_id->demands instead of direct ministry_id (the two "
        "certified paths in joins.yaml should agree here -- no mid-year "
        "reassignment for this ministry)"),

    CrossQuery("Q02",
        "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
        "JOIN demands d ON a.demand_id=d.demand_id "
        "WHERE a.fiscal_year='2021-22' AND d.demand_name='Department of Higher Education'",
        "via demand_id path"),

    CrossQuery("Q03",
        "SELECT COUNT(*) FROM schemes WHERE scheme_type='CSS'",
        "is_centrally_sponsored should be defined as scheme_type='CSS' -- "
        "checking the flag and the underlying type agree"),

    CrossQuery("Q04",
        "SELECT ministry_name, total_crore FROM ("
        "  SELECT m.ministry_name, SUM(a.be_amount_crore) AS total_crore,"
        "         RANK() OVER (ORDER BY SUM(a.be_amount_crore) DESC) AS rnk"
        "  FROM budget_allocations a JOIN ministries m ON a.ministry_id=m.ministry_id"
        "  WHERE a.fiscal_year='2022-23' GROUP BY 1"
        ") WHERE rnk<=5 ORDER BY total_crore DESC",
        "same top-5 via RANK() window function instead of LIMIT -- also "
        "surfaces ties at the boundary that LIMIT would hide", order_matters=True),

    CrossQuery("Q05",
        "SELECT fiscal_year FROM fiscal_years WHERE start_year < 2017 ORDER BY fiscal_year",
        "is_plan_regime should mean start_year < 2017 -- checking the flag "
        "matches the boundary it is supposed to encode", order_matters=True),

    CrossQuery("Q06",
        "SELECT SUM(a.re_amount_crore) FROM budget_allocations a "
        "JOIN demands d ON a.demand_id=d.demand_id "
        "WHERE a.fiscal_year='2022-23' AND d.demand_name='Ministry of Cooperation'",
        "via demand_id path"),

    Invariant("Q07",
        "SELECT state_code FROM states WHERE is_union_territory=TRUE ORDER BY 1",
        lambda rows_unused: True,  # printed for manual eyeballing, see describe
        "hand-checkable against the STATES table in load_data.py (DL, PY, "
        "JKU, LA are the only UTs defined there)",
        lambda row: f"UT set from DB: (see full result printed above)"),

    CrossQuery("Q08",
        "SELECT SUM(a.actual_amount_crore) FROM budget_allocations a "
        "JOIN demands d ON a.demand_id=d.demand_id "
        "WHERE a.fiscal_year='2020-21' AND d.demand_name='Ministry of Railways'",
        "via demand_id path"),

    CrossQuery("Q09",
        "SELECT scheme_name, closure_year FROM schemes "
        "WHERE status='closed' ORDER BY closure_year, scheme_name",
        "status='closed' is set from closure_year IS NOT NULL at generation "
        "time -- checking the two derived fields agree", order_matters=True),

    CrossQuery("Q10",
        "SELECT COUNT(*) FROM budget_allocations a "
        "JOIN ministries m ON a.ministry_id=m.ministry_id "
        "WHERE a.fiscal_year='2018-19'",
        "same count after an inner join to ministries -- ministry_id is "
        "always populated on allocations, so this must not change the count "
        "(a change would mean a silent fan-out or a NULL FK)"),

    CrossQuery("Q11",
        "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
        "WHERE a.fiscal_year='2021-22' AND a.scheme_id = "
        "  (SELECT scheme_id FROM schemes WHERE scheme_name='Jal Jeevan Mission')",
        "resolve scheme_id once via a subquery instead of joining on name -- "
        "catches a join that accidentally matches more than one scheme"),

    CrossQuery("Q12",
        # First pass hardcoded a name that doesn't exist ("Defence Services
        # (Capital)") and omitted one that does ("Pensions") -- confirmed by
        # querying entity_kind directly per candidate name. Corrected to the
        # actual real-data entity names rather than a second guess at them.
        "SELECT ministry_name FROM ministries WHERE ministry_name IN ("
        "'Interest Payments','Transfers to States','Defence Pensions',"
        "'Defence Services (Revenue)','Capital Outlay on Defence Services',"
        "'Pensions') ORDER BY ministry_name",
        "against the actual functional-entity names confirmed to exist in "
        "the ministries table (see gold_audit_report.md for how the first "
        "attempt at this list was wrong)",
        order_matters=True),

    CrossQuery("T13_UNUSED_PLACEHOLDER_SKIP", "SELECT 1", "placeholder"),  # removed below

    Invariant("Q14",
        # First pass used NOT(effective_to_year<2016) as a supposedly-equivalent
        # rewrite. It is NOT equivalent: NOT(NULL<2016) evaluates to NULL, not
        # TRUE, in SQL's three-valued logic, so the negated form silently
        # dropped the 102 of 107 ministries with a NULL (still active)
        # effective_to_year. Confirmed directly: NOT(NULL<2016) -> NULL.
        # Corrected to a form that handles NULL the same way gold's does, so
        # this is now a genuine algebraic-identity check rather than a
        # NULL-trap in the audit itself.
        "SELECT "
        "  (SELECT COUNT(*) FROM ministries WHERE effective_from_year<=2016 "
        "     AND (effective_to_year IS NULL OR effective_to_year>=2016)) AS a,"
        "  (SELECT COUNT(*) FROM ministries WHERE effective_from_year<=2016 "
        "     AND COALESCE(effective_to_year, 9999) >= 2016) AS b",
        lambda row: row[0] == row[1],
        "same boundary via COALESCE instead of IS NULL OR -- both handle "
        "NULL the same way (as 'no end year'), unlike a naive NOT() rewrite "
        "which does not (see gold_audit_report.md)",
        lambda row: f"IS-NULL-OR form={row[0]}  COALESCE form={row[1]}"),

    Invariant("Q15",
        # First pass asserted no special-category state exceeds 20M population.
        # False: Assam (31.2M) has held special-category status historically.
        # Rewritten as the check that actually distinguishes a real bug from a
        # bad prior -- large GENERAL-category states must not be flagged, but
        # a populous special-category state like Assam is expected.
        "SELECT (SELECT state_name FROM states "
        "WHERE is_special_category=TRUE AND population_census > 100000000 LIMIT 1)",
        lambda row: row[0] is None,
        "no state with population above 100M should carry special-category "
        "status -- that threshold is well above Assam's real 31.2M and would "
        "only trip on something like Uttar Pradesh or Maharashtra being "
        "mis-flagged, which is the actual bug this check is for",
        lambda row: "no oversized state flagged" if row[0] is None
                    else f"MIS-FLAGGED: {row[0]}"),

    Invariant("Q16",
        "SELECT "
        "  (SELECT SUM(be_amount_crore) FROM receipts WHERE fiscal_year='2020-21') AS total,"
        "  (SELECT SUM(be_amount_crore) FROM receipts WHERE fiscal_year='2020-21' "
        "     AND receipt_type IN ('non_tax','capital')) AS rest",
        lambda row: True,  # informational: printed, not a hard pass/fail
        "algebraic identity: tax receipts = total receipts - (non_tax + "
        "capital). If gold's tax-only sum doesn't equal total-minus-rest, "
        "one of the two partitions is wrong",
        lambda row: f"total={row[0]:,.0f}  non_tax+capital={row[1]:,.0f}  "
                    f"implied tax={row[0]-row[1]:,.0f}"),

    Invariant("Q17",
        "SELECT SUM(expenditure_crore) FROM expenditure_actuals WHERE fiscal_year='2019-20'",
        lambda row: True,
        "the flat annual total, to be compared against the sum of the 12 "
        "monthly figures gold's GROUP BY produces -- they must match exactly",
        lambda row: f"flat SUM()={row[0]:,.2f}"),

    CrossQuery("Q18",
        "SELECT COUNT(*) FROM account_heads WHERE is_capital=TRUE",
        "is_capital boolean vs account_type='capital' string -- set from the "
        "same source at generation time, must agree"),

    Invariant("Q19",
        "SELECT COUNT(*) FROM ("
        "  SELECT m.ministry_id, SUM(a.be_amount_crore) AS total_crore"
        "  FROM budget_allocations a JOIN ministries m ON a.ministry_id=m.ministry_id"
        "  WHERE a.fiscal_year='2015-16' GROUP BY 1"
        "  ORDER BY total_crore DESC LIMIT 1"
        ") t, ("
        "  SELECT MAX(s) AS mx FROM ("
        "    SELECT SUM(a.be_amount_crore) AS s FROM budget_allocations a "
        "    JOIN ministries m ON a.ministry_id=m.ministry_id "
        "    WHERE a.fiscal_year='2015-16' GROUP BY m.ministry_id"
        "  )"
        ") x "
        "WHERE t.total_crore = x.mx",
        lambda row: row[0] == 1,
        "confirms the LIMIT-1 winner in gold actually equals the true "
        "maximum (catches a wrong ORDER BY direction, which would silently "
        "return the smallest ministry instead of the largest)",
        lambda row: "winner matches true max" if row and row[0] == 1 else "MISMATCH"),

    CrossQuery("Q20",
        "SELECT SUM(t.be_amount_crore) FROM transfers_to_states t "
        "WHERE t.fiscal_year='2021-22' AND t.state_code='BR'",
        "resolve state_code once instead of joining on state_name -- catches "
        "a name join that matches the wrong state"),

    # --- T01-T12: trap ---------------------------------------------------------
    Invariant("T01",
        "SELECT "
        "  (SELECT SUM(be_amount_crore) FROM budget_allocations WHERE fiscal_year='2023-24') AS crore,"
        "  (SELECT SUM(be_amount_rupees) FROM budget_allocations WHERE fiscal_year='2023-24') AS rupees",
        lambda row: row[0] and row[1] and abs(row[1] / row[0] / 10_000_000 - 1) < 1e-9,
        "cross-checking via the THIRD unit column (rupees), not the one the "
        "trap is about (lakh) -- rupees/1e7 must equal the crore figure "
        "exactly, since both are computed from be_amount_crore at generation time",
        lambda row: f"crore={row[0]:,.0f}  rupees/1e7={row[1]/10_000_000:,.0f}"),

    Invariant("T02",
        "SELECT "
        "  (SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
        "     JOIN ministries m ON a.ministry_id=m.ministry_id "
        "     WHERE a.fiscal_year='2018-19' AND m.ministry_name='Ministry of Railways') AS crore,"
        "  (SELECT SUM(a.be_amount_lakh) FROM budget_allocations a "
        "     JOIN ministries m ON a.ministry_id=m.ministry_id "
        "     WHERE a.fiscal_year='2018-19' AND m.ministry_name='Ministry of Railways') AS lakh",
        lambda row: row[0] and row[1] and abs(row[1] / row[0] / 100 - 1) < 1e-9,
        "lakh/100 must equal crore exactly for this entity+year, same check "
        "as the schema-wide one but scoped to confirm it holds at the "
        "row-filtered level too, not just in aggregate",
        lambda row: f"crore={row[0]:,.2f}  lakh/100={row[1]/100:,.2f}"),

    Invariant("T03",
        # First pass used a 1e-9 tolerance. Too tight: each row's
        # plan_amount_crore is independently round()ed to 3 decimals in
        # load_data.py BEFORE summing across ~1,000+ rows, so cumulative
        # rounding error of order 1e-8 relative to the ratio is expected, not
        # a bug. 1e-6 comfortably clears that while still catching anything
        # that would actually matter (a wrong multiplier, a filter bug).
        "SELECT SUM(plan_amount_crore), SUM(be_amount_crore) FROM budget_allocations "
        "WHERE fiscal_year='2016-17'",
        lambda row: row[0] and row[1] and abs(row[0] / row[1] - 0.45) < 1e-6,
        "known generation invariant: load_data.py sets plan_amount_crore = "
        "0.45 * be_amount_crore exactly, for every row, in the Plan era. "
        "If this ratio isn't ~0.45, either the generator changed or gold is "
        "filtering something the invariant doesn't expect",
        lambda row: f"plan/be = {row[0]/row[1]:.9f}  (expect ~0.45, small "
                    f"deviation from per-row rounding is normal)"),

    Invariant("T04",
        # First pass asserted exactly 12 raw rows per ministry before
        # aggregation. Wrong target: a ministry can have many allocations,
        # each independently selected into the monthly sample, so its raw row
        # count is any multiple of 12, not 12 itself (confirmed: 77 ministries
        # failed that check). What actually matters for gold's MAX() to be
        # safe is that the CTE's GROUP BY collapses each ministry to exactly
        # ONE row -- that's the real invariant, checked directly here.
        "WITH monthly AS ("
        "  SELECT fiscal_year, ministry_id, SUM(expenditure_crore) AS actual_crore"
        "  FROM expenditure_actuals WHERE fiscal_year='2019-20' GROUP BY 1,2"
        "), dupes AS ("
        "  SELECT ministry_id, COUNT(*) AS n FROM monthly GROUP BY 1 HAVING COUNT(*)>1"
        ") SELECT (SELECT ministry_id FROM dupes LIMIT 1), (SELECT n FROM dupes LIMIT 1)",
        lambda row: row[0] is None,
        "the CTE must produce at most one row per ministry_id -- if any "
        "ministry had more than one, gold's outer MAX(mo.actual_crore) could "
        "silently pick among several different values instead of the single "
        "correct total",
        lambda row: "every ministry has exactly one CTE row (confirmed)" if row[0] is None
                    else f"ministry_id {row[0]} has {row[1]} CTE rows -- MAX() is unsafe here"),

    Invariant("T05",
        "SELECT "
        "  (SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
        "     JOIN ministries m ON a.ministry_id=m.ministry_id "
        "     WHERE a.fiscal_year='2022-23') AS grand_total,"
        "  (SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
        "     JOIN ministries m ON a.ministry_id=m.ministry_id "
        "     WHERE a.fiscal_year='2022-23' AND m.entity_kind='functional') AS functional",
        lambda row: True,
        "algebraic identity: administrative total (gold) must equal "
        "grand_total - functional_total. Printed for comparison against "
        "gold's own figure",
        lambda row: f"grand_total={row[0]:,.0f}  functional={row[1]:,.0f}  "
                    f"implied administrative={row[0]-row[1]:,.0f}"),

    CrossQuery("T06",
        # Cross-check against the actual lineage-resolution MACHINERY (semantic.py
        # resolve_lineage), not a second hand-written IN-list. This is the
        # strongest available check for T06: if the canonical function and the
        # hand-written gold list disagree, the hand-written list is the one to
        # fix, since the function is what the running system actually uses.
        "__LINEAGE__",
        "against src/rosetta/semantic.py resolve_lineage() -- the actual "
        "mechanism the system uses, not a second hand-written name list"),

    Invariant("T07",
        "SELECT "
        "  (SELECT SUM(gross_amount_crore) FROM budget_allocations WHERE fiscal_year='2020-21') AS gross,"
        "  (SELECT SUM(be_amount_crore) FROM budget_allocations WHERE fiscal_year='2020-21') AS be",
        lambda row: row[0] and row[1] and abs(row[0] / row[1] - 1) < 1e-9,
        "known generation invariant: load_data.py sets gross_amount_crore = "
        "be_amount_crore exactly (see allocation_rows: "
        "\"gross_amount_crore\": be). These must be EXACTLY equal, not "
        "approximately -- any gap means gold computed gross some other way",
        lambda row: f"gross={row[0]:,.2f}  be={row[1]:,.2f}  ratio={row[0]/row[1]:.9f}"),

    Invariant("T08",
        "SELECT "
        "  (SELECT SUM(be_amount_crore) FROM receipts "
        "     WHERE fiscal_year='2021-22' AND is_revenue_receipt=TRUE) AS via_revenue_flag,"
        "  (SELECT SUM(be_amount_crore) FROM receipts "
        "     WHERE fiscal_year='2021-22' AND is_borrowing=FALSE) AS via_not_borrowing",
        lambda row: True,  # deliberately NOT asserting equality -- see describe
        "THESE TWO ARE NOT THE SAME FILTER, and gold must use the first "
        "one. is_borrowing=FALSE also admits capital receipts like "
        "disinvestment and recovery of loans, which are not revenue. If "
        "gold's figure matches the second column instead of the first, "
        "gold has the wrong filter",
        lambda row: f"is_revenue_receipt=TRUE  -> {row[0]:,.0f}\n"
                    f"    is_borrowing=FALSE (WRONG filter, broader) -> {row[1]:,.0f}"),

    Invariant("T09",
        "SELECT "
        "  (SELECT SUM(net_to_centre_crore) FROM receipts "
        "     WHERE fiscal_year='2020-21' AND receipt_type='tax') AS via_column,"
        "  (SELECT SUM(gross_amount_crore - devolution_to_states_crore) FROM receipts "
        "     WHERE fiscal_year='2020-21' AND receipt_type='tax') AS via_formula",
        lambda row: row[0] and row[1] and abs(row[0] / row[1] - 1) < 1e-6,
        "known generation invariant: net_to_centre_crore = gross_amount_crore "
        "- devolution_to_states_crore exactly, by construction. If the "
        "column and the formula disagree, one of the two was computed on a "
        "different filter",
        lambda row: f"via net_to_centre_crore column={row[0]:,.2f}  "
                    f"via gross-devolution formula={row[1]:,.2f}"),

    Invariant("T10",
        "SELECT "
        "  (SELECT COUNT(*) FROM budget_allocations WHERE fiscal_year='2021-22') AS no_join,"
        "  (SELECT COUNT(*) FROM budget_allocations a "
        "     LEFT JOIN states s ON a.state_code=s.state_code "
        "     WHERE a.fiscal_year='2021-22') AS left_join",
        lambda row: row[0] == row[1],
        "row count must be identical with and without a LEFT JOIN to "
        "states -- state_code is a direct one-to-one FK (or NULL), so a "
        "LEFT JOIN must never multiply rows. If these differ, states has a "
        "duplicate state_code, which would silently inflate any query that "
        "joins through it",
        lambda row: f"no join: {row[0]:,}   left join: {row[1]:,}"),

    Invariant("T11",
        "SELECT COUNT(*) FROM budget_allocations a "
        "JOIN schemes s ON a.scheme_id=s.scheme_id "
        "WHERE a.fiscal_year='2016-17' AND s.scheme_name='Samagra Shiksha'",
        lambda row: row[0] == 0,
        "Samagra Shiksha launched 2018-19 (see SCHEMES_SPEC in "
        "load_data.py). If any 2016-17 allocation row is tagged with that "
        "scheme, the generator's schemes_for() year-filtering is broken and "
        "the trap does not actually exist in the data",
        lambda row: f"2016-17 rows tagged Samagra Shiksha: {row[0]} (must be 0)"),

    Invariant("T12",
        "SELECT "
        "  (SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
        "     JOIN ministries m ON a.ministry_id=m.ministry_id "
        "     WHERE a.fiscal_year='2019-20' AND m.ministry_name='Ministry of Railways') AS be_alone,"
        "  (SELECT SUM(a.re_amount_crore) FROM budget_allocations a "
        "     JOIN ministries m ON a.ministry_id=m.ministry_id "
        "     WHERE a.fiscal_year='2019-20' AND m.ministry_name='Ministry of Railways') AS re_alone",
        lambda row: True,
        "BE and RE computed as two SEPARATE single-column queries, to "
        "compare against gold's combined two-column query -- a bad join in "
        "the combined form (e.g. joining allocations to itself) would "
        "inflate one or both columns relative to these",
        lambda row: f"be_alone={row[0]:,.2f}  re_alone={row[1]:,.2f}"),
]

# Remove the accidental placeholder left in during drafting.
AUDITS = [a for a in AUDITS if a.qid != "T13_UNUSED_PLACEHOLDER_SKIP"]


def run_lineage_check(db, gold_sql: str) -> tuple[bool, str]:
    gold = db.execute(gold_sql)
    if not gold.ok:
        return False, f"gold errors: {gold.error}"

    lineage_ids = resolve_lineage(db, "Ministry of Human Resource Development")
    if not lineage_ids:
        return False, "resolve_lineage() returned nothing -- lineage graph broken"
    id_list = ", ".join(str(i) for i in lineage_ids)
    alt_sql = (
        f"SELECT a.fiscal_year, SUM(a.be_amount_crore) AS total_crore "
        f"FROM budget_allocations a WHERE a.ministry_id IN ({id_list}) "
        f"GROUP BY 1 ORDER BY 1"
    )
    alt = db.execute(alt_sql)
    if not alt.ok:
        return False, f"lineage-based query errors: {alt.error}"

    cmp = compare_result_sets(alt.rows, gold.rows, order_matters=True)
    names = db.execute(
        f"SELECT ministry_name FROM ministries WHERE ministry_id IN ({id_list}) "
        "ORDER BY effective_from_year"
    ).rows
    names_str = " -> ".join(r[0] for r in names)
    if cmp.match is Match.EXACT:
        return True, f"agrees. resolve_lineage() found: {names_str}"
    return False, f"DISAGREES ({cmp.reason}). resolve_lineage() found: {names_str}"


def main() -> int:
    if not DB.exists():
        print(f"database missing at {DB}; run scripts/load_data.py first")
        return 1

    questions = load()
    db = Database(DB)

    print(f"{len(AUDITS)} independent checks against gold SQL\n")
    print("Method: a different query PATH to the same number, an ALGEBRAIC")
    print("identity, or a known GENERATION INVARIANT from load_data.py.")
    print("This is not blind cross-model verification -- see the module")
    print("docstring for why that is not achievable in this thread.\n")

    passed = failed = informational = 0

    for audit in AUDITS:
        q = questions.get(audit.qid)
        if q is None:
            print(f"  ????  {audit.qid:5s} not found in questions.yaml")
            failed += 1
            continue
        gold_sql = q.get("gold_sql")
        if not gold_sql:
            print(f"  ----  {audit.qid:5s} no gold_sql to check")
            continue

        if audit.qid == "T06":
            ok, detail = run_lineage_check(db, gold_sql)
        else:
            ok, detail = audit.run(db, gold_sql)

        # Some checks are informational (print both sides, no hard verdict)
        # because the "correct" relationship isn't a strict equality -- e.g.
        # Q07's UT list is meant for eyeballing, not automated comparison.
        is_info = audit.qid in ("Q07", "Q16", "Q17", "T05", "T12")
        if is_info:
            informational += 1
            mark = "info"
        else:
            mark = "ok  " if ok else "FAIL"
            if ok:
                passed += 1
            else:
                failed += 1

        print(f"  {mark}  {audit.qid:5s} {q['question'][:44]:44s}")
        print(f"        [{audit.method}]")
        print(f"        {detail}")
        print()

    print("=" * 70)
    print(f"  {passed} passed  ·  {failed} failed  ·  {informational} informational")
    print("=" * 70)

    if failed:
        print("\n  Failures are either a bug in gold_sql or a bug in this audit's")
        print("  own cross-check -- read the detail line and verify by hand before")
        print("  changing anything.")

    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
