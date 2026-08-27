"""Adjudicate the 28 refusal categorizations with evidence.

These were the open item: 16 `ambiguous` + 12 `unanswerable` questions whose
classification rests on judgement rather than on a gold query that can be
executed. This script replaces "trust me" with measurement wherever
measurement is possible, and isolates the residue that genuinely is not.

Three passes:

  1. UNANSWERABLE (12). Not judgement at all -- these rest on facts about what
     the data contains. Each is checked directly: does the column actually
     hold NULL for that year, is the year actually outside the Plan era. A
     question is only unanswerable if the data says so.

  2. AMBIGUOUS (16). For each, compute what the COMPETING READINGS actually
     return, and report the spread between them. This turns "is this
     ambiguous?" from an opinion into a number. A question whose readings
     differ by 0.4% is pedantic to refuse; one whose readings differ 5x is
     obviously right to refuse. The threshold is a policy choice, but the
     spread is a fact.

  3. RESIDUE. What is left after 1 and 2 -- the genuinely arguable calls,
     named explicitly with the assumption each rests on.

    python scripts/adjudicate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta.db import Database  # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.yaml"
DB = ROOT / "data" / "rosetta.duckdb"


# ---------------------------------------------------------------------------
# Pass 1: the 12 unanswerable -- pure fact checks
# ---------------------------------------------------------------------------
# (question id, human claim, SQL that must return 0 for the claim to hold)
UNANSWERABLE_CHECKS = [
    ("U01", "no Plan expenditure exists for 2019-20",
     "SELECT COUNT(plan_amount_crore) FROM budget_allocations WHERE fiscal_year='2019-20'"),
    ("U02", "no Plan expenditure exists for 2021-22",
     "SELECT COUNT(plan_amount_crore) FROM budget_allocations WHERE fiscal_year='2021-22'"),
    ("U03", "no actuals exist for 2023-24",
     "SELECT COUNT(actual_amount_crore) FROM budget_allocations WHERE fiscal_year='2023-24'"),
    ("U04", "no actuals exist for 2022-23 (Railways or any entity)",
     "SELECT COUNT(actual_amount_crore) FROM budget_allocations WHERE fiscal_year='2022-23'"),
    ("U05", "no actuals exist for 2023-24 (education or any entity)",
     "SELECT COUNT(actual_amount_crore) FROM budget_allocations WHERE fiscal_year='2023-24'"),
    ("U06", "no Revised Estimate exists for 2023-24",
     "SELECT COUNT(re_amount_crore) FROM budget_allocations WHERE fiscal_year='2023-24'"),
    ("U07", "no Plan outlay exists for 2018-19",
     "SELECT COUNT(plan_amount_crore) FROM budget_allocations WHERE fiscal_year='2018-19'"),
    ("U08", "no actuals exist for 2023-24 (MGNREGA or any scheme)",
     "SELECT COUNT(actual_amount_crore) FROM budget_allocations WHERE fiscal_year='2023-24'"),
    ("U09", "no Non-Plan expenditure exists for 2020-21",
     "SELECT COUNT(non_plan_amount_crore) FROM budget_allocations WHERE fiscal_year='2020-21'"),
    ("U10", "no actuals exist for 2022-23 (by ministry or otherwise)",
     "SELECT COUNT(actual_amount_crore) FROM budget_allocations WHERE fiscal_year='2022-23'"),
    ("U11", "no Plan expenditure exists for 2022-23",
     "SELECT COUNT(plan_amount_crore) FROM budget_allocations WHERE fiscal_year='2022-23'"),
    ("U12", "no Revised Estimate exists for 2023-24 (Cooperation or otherwise)",
     "SELECT COUNT(re_amount_crore) FROM budget_allocations WHERE fiscal_year='2023-24'"),
]


# ---------------------------------------------------------------------------
# Pass 2: the 16 ambiguous -- measure the spread between competing readings
# ---------------------------------------------------------------------------
# (id, dimension, [(reading label, sql), ...])
def be(year, extra=""):
    return (f"SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
            f"JOIN ministries m ON a.ministry_id=m.ministry_id "
            f"WHERE a.fiscal_year='{year}' {extra}")


def basis_readings(year, extra=""):
    return [
        ("Budget Estimate", be(year, extra)),
        ("Revised Estimate", be(year, extra).replace("be_amount_crore", "re_amount_crore")),
        ("Actuals", be(year, extra).replace("be_amount_crore", "actual_amount_crore")),
    ]


def scope_readings(year):
    return [
        ("all spending", be(year)),
        ("ministries + departments only",
         be(year, "AND m.entity_kind IN ('ministry','department')")),
        ("entities named 'ministry' only", be(year, "AND m.entity_kind='ministry'")),
    ]


def scheme_readings(year, names):
    return [
        (name,
         f"SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
         f"JOIN schemes s ON a.scheme_id=s.scheme_id "
         f"WHERE a.fiscal_year='{year}' AND s.scheme_name='{name}'")
        for name in names
    ]


SBM = ["Swachh Bharat Mission - Gramin", "Swachh Bharat Mission - Urban"]
PMAY = ["Pradhan Mantri Awaas Yojana - Gramin", "Pradhan Mantri Awaas Yojana - Urban"]

AMBIGUOUS_READINGS = [
    ("A01", "basis + scope", basis_readings("2019-20") + scope_readings("2019-20")),
    ("A02", "basis", basis_readings("2020-21", "AND m.ministry_name='Ministry of Railways'")),
    ("A03", "scheme identity", scheme_readings("2019-20", SBM)),
    ("A04", "basis + scope", basis_readings("2021-22") + scope_readings("2021-22")),
    ("A05", "scheme identity", scheme_readings("2020-21", PMAY)),
    ("A06", "entity identity", [
        ("School Education (incl. predecessor MHRD)",
         "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
         "JOIN ministries m ON a.ministry_id=m.ministry_id WHERE m.ministry_name IN "
         "('Ministry of Human Resource Development','Department of School Education and Literacy')"),
        ("Higher Education",
         "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
         "JOIN ministries m ON a.ministry_id=m.ministry_id "
         "WHERE m.ministry_name='Department of Higher Education'"),
    ]),
    ("A07", "scheme identity", scheme_readings("2021-22", PMAY)),
    ("A08", "basis + scope", basis_readings("2018-19") + scope_readings("2018-19")),
    ("A09", "basis", basis_readings("2017-18", "AND m.ministry_name='Ministry of Railways'")),
    ("A10", "receipts scope", [
        ("revenue receipts only",
         "SELECT SUM(be_amount_crore) FROM receipts WHERE fiscal_year='2020-21' "
         "AND is_revenue_receipt=TRUE"),
        ("total receipts incl. borrowings",
         "SELECT SUM(be_amount_crore) FROM receipts WHERE fiscal_year='2020-21'"),
    ]),
    ("A11", "basis + scope (all years)", [
        ("Budget Estimate, all spending",
         "SELECT SUM(be_amount_crore) FROM budget_allocations"),
        ("Revised Estimate, all spending",
         "SELECT SUM(re_amount_crore) FROM budget_allocations"),
        ("Actuals, all spending (7 of 10 years only)",
         "SELECT SUM(actual_amount_crore) FROM budget_allocations"),
    ]),
    ("A12", "entity identity", [
        ("Water Resources (incl. predecessor)",
         "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
         "JOIN ministries m ON a.ministry_id=m.ministry_id WHERE m.ministry_name IN "
         "('Ministry of Water Resources',"
         "'Department of Water Resources, River Development and Ganga Rejuvenation')"),
        ("Drinking Water and Sanitation",
         "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
         "JOIN ministries m ON a.ministry_id=m.ministry_id "
         "WHERE m.ministry_name='Department of Drinking Water and Sanitation'"),
    ]),
    ("A13", "scheme identity", scheme_readings("2018-19", SBM)),
    ("A14", "basis", basis_readings("2020-21", "AND m.sector='rural'")),
    ("A15", "basis + scope", basis_readings("2016-17") + scope_readings("2016-17")),
    ("A16", "basis + scope", basis_readings("2015-16") + scope_readings("2015-16")),
]


def spread(values: list[float]) -> float:
    live = [v for v in values if v is not None and v > 0]
    if len(live) < 2:
        return 0.0
    return max(live) / min(live)


def main() -> int:
    if not DB.exists():
        print(f"database missing at {DB}; run scripts/load_data.py first")
        return 1

    questions = {q["id"]: q for q in yaml.safe_load(
        QUESTIONS.read_text(encoding="utf-8"))["questions"]}
    db = Database(DB)

    # --- Pass 1 -----------------------------------------------------------
    print("=" * 74)
    print("  PASS 1 -- the 12 'unanswerable': fact checks, not judgement")
    print("=" * 74)
    print("  Each claims the data does not exist. Verified directly.\n")
    u_ok = u_bad = 0
    for qid, claim, sql in UNANSWERABLE_CHECKS:
        result = db.execute(sql)
        n = result.rows[0][0] if result.ok and result.rows else -1
        ok = n == 0
        u_ok += ok
        u_bad += (not ok)
        print(f"  {'ok  ' if ok else 'WRONG'}  {qid}  {claim}")
        print(f"        non-NULL rows found: {n}   (must be 0)")
    print(f"\n  {u_ok}/12 verified correct, {u_bad} wrong")

    # --- Pass 2 -----------------------------------------------------------
    print()
    print("=" * 74)
    print("  PASS 2 -- the 16 'ambiguous': how far apart are the readings?")
    print("=" * 74)
    print("  Refusing is defensible in proportion to the spread. Measured,")
    print("  not asserted.\n")

    rows = []
    for qid, dimension, readings in AMBIGUOUS_READINGS:
        values = []
        for label, sql in readings:
            r = db.execute(sql)
            v = r.rows[0][0] if r.ok and r.rows else None
            values.append((label, v))
        ratio = spread([v for _, v in values])
        rows.append((qid, dimension, ratio, values))

    for qid, dimension, ratio, values in sorted(rows, key=lambda x: -x[2]):
        verdict = ("REFUSE clearly right" if ratio >= 1.5
                   else "refuse defensible" if ratio >= 1.10
                   else "REFUSAL IS PEDANTIC" if ratio > 1.0
                   else "readings identical")
        print(f"  {qid}  [{dimension:24s}]  spread {ratio:>5.2f}x   {verdict}")
        for label, v in values:
            shown = f"{v:>14,.0f}" if v is not None else f"{'no data':>14s}"
            print(f"        {label:<44s} {shown}")
        print()

    db.close()

    # --- Pass 3 -----------------------------------------------------------
    print("=" * 74)
    print("  PASS 3 -- what is left that measurement cannot settle")
    print("=" * 74)
    tight = [r for r in rows if 1.0 < r[2] < 1.10]
    wide = [r for r in rows if r[2] >= 1.5]
    mid = [r for r in rows if 1.10 <= r[2] < 1.5]
    print(f"""
  {len(wide):>2} questions: readings differ by 1.5x or more.
      Refusing is not a judgement call at this spread -- answering with
      one reading silently and unlabelled is indefensible when another
      defensible reading is half or double the number.

  {len(mid):>2} questions: readings differ 1.10x - 1.5x.
      Still a real difference. A 10-20% error in a budget figure is a
      headline correction, not a rounding note.

  {len(tight):>2} questions: readings differ by less than 10%.
      THIS is the genuine judgement call. At this spread, refusing costs
      the user a round-trip to avoid an error they might not care about.
      Whether that trade is right depends on who the reader is -- an
      analyst who knows BE from RE, or someone about to print the number.
""")
    return 1 if u_bad else 0


if __name__ == "__main__":
    sys.exit(main())
