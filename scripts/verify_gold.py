"""Verify the gold evaluation set.

Two independent checks, and both must pass before any metric from this project
is quotable:

  1. Every gold_sql executes and returns a sensible result. A gold query that
     errors is obvious; a gold query that runs and returns the WRONG rows is
     not, and it silently corrupts every number downstream. This checks the
     first and flags suspicious shapes for the second -- empty results, single
     NULLs, and magnitudes outside a plausible range for the Union Budget.

  2. Every question is classified the way the eval expects. If ambiguity.assess
     answers a question the gold set says to refuse, the refusal rules and the
     gold set disagree, and one of them is wrong. That disagreement has to be
     resolved by a human, not averaged over.

    python scripts/verify_gold.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta import ambiguity                      # noqa: E402
from rosetta.compare import gold_requires_order    # noqa: E402
from rosetta.db import Database                    # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.yaml"
DB = ROOT / "data" / "rosetta.duckdb"

# The 2024-25 Union Budget grand total is about 4.77 lakh crore. Anything a
# gold query returns that is wildly outside a plausible band for a budget
# figure is worth a human look, even though it is not necessarily wrong.
PLAUSIBLE_MAX_CRORE = 20_000_000
SUSPICIOUS_MIN_CRORE = 0.001


def load_questions() -> list[dict]:
    data = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    return data.get("questions", [])


def looks_suspicious(rows, columns) -> str | None:
    if not rows:
        return "returned no rows"
    if len(rows) == 1 and len(rows[0]) == 1:
        value = rows[0][0]
        if value is None:
            return "returned a single NULL"
        if isinstance(value, (int, float)):
            if value == 0:
                return "returned zero"
            if abs(value) > PLAUSIBLE_MAX_CRORE:
                return f"magnitude {value:,.0f} looks too large for a crore figure"
            if 0 < abs(value) < SUSPICIOUS_MIN_CRORE:
                return f"magnitude {value} looks too small"
    return None


def verify_traps(db: Database) -> int:
    """Check that every designed ambiguity is still present in the data.

    The README claims six specific traps exist. Claims about data rot when the
    generator changes, so they are checked rather than asserted -- a project
    about confidently wrong numbers should not carry an unverified one in its
    own front matter.
    """
    failures = 0

    def check(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:18s} {detail}")

    # 1. units: lakh is exactly 100x crore
    row = db.execute(
        "SELECT SUM(be_amount_crore), SUM(be_amount_lakh) FROM budget_allocations"
    ).rows[0]
    ratio = row[1] / row[0] if row[0] else 0
    check("units", abs(ratio - 100) < 1e-6, f"lakh/crore = {ratio:.4f}x")

    # 2. plan era: populated before 2017-18, NULL after
    inside = db.execute(
        "SELECT count(plan_amount_crore) FROM budget_allocations "
        "WHERE fiscal_year IN ('2014-15','2015-16','2016-17')"
    ).rows[0][0]
    outside = db.execute(
        "SELECT count(plan_amount_crore) FROM budget_allocations "
        "WHERE fiscal_year NOT IN ('2014-15','2015-16','2016-17')"
    ).rows[0][0]
    check("plan_era", inside > 0 and outside == 0,
          f"{inside:,} rows in era, {outside} outside")

    # 3. fan-out: the forbidden join inflates
    correct = db.execute(
        "SELECT SUM(be_amount_crore) FROM budget_allocations WHERE fiscal_year='2019-20'"
    ).rows[0][0]
    naive = db.execute(
        "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
        "JOIN expenditure_actuals e ON a.ministry_id=e.ministry_id "
        "AND a.fiscal_year=e.fiscal_year WHERE a.fiscal_year='2019-20'"
    ).rows[0][0]
    factor = naive / correct if correct else 0
    check("fan_out", factor > 5, f"naive join inflates {factor:,.0f}x")

    # 4. entity granularity: functional lines are a large share
    rows = db.execute(
        "SELECT m.entity_kind, SUM(a.be_amount_crore) FROM budget_allocations a "
        "JOIN ministries m ON a.ministry_id=m.ministry_id "
        "WHERE a.fiscal_year='2022-23' GROUP BY 1"
    ).rows
    totals = {r[0]: r[1] for r in rows}
    functional = totals.get("functional", 0)
    grand = sum(totals.values())
    share = functional / grand if grand else 0
    check("entity_scope", 0.2 < share < 0.7,
          f"functional lines are {share:.0%} of total")

    # 5. renames: predecessor and successor both carry money
    lineage = db.execute(
        "SELECT count(*) FROM ministries WHERE renamed_from_id IS NOT NULL"
    ).rows[0][0]
    check("rename", lineage > 0, f"{lineage} entities have a predecessor")

    # 6. gross vs net actually differ
    row = db.execute(
        "SELECT SUM(gross_amount_crore), SUM(net_amount_crore) FROM budget_allocations"
    ).rows[0]
    gap = (row[0] - row[1]) / row[0] if row[0] else 0
    check("gross_net", gap > 0.005, f"net is {gap:.1%} below gross")

    return failures


def main() -> int:
    if not DB.exists():
        print(f"database missing at {DB}; run scripts/load_data.py first")
        return 1

    questions = load_questions()
    db = Database(DB)

    sql_ok = sql_fail = 0
    suspicious: list[tuple[str, str]] = []
    class_ok = class_fail = 0
    mismatches: list[tuple[str, str, str, str]] = []

    print(f"{len(questions)} questions\n")
    print("--- 1. gold SQL executes ---")

    for q in questions:
        qid = q["id"]
        gold = q.get("gold_sql")
        if not gold:
            continue
        result = db.execute(gold)
        if not result.ok:
            sql_fail += 1
            print(f"  FAIL  {qid}  {result.status.value}: {(result.error or '')[:90]}")
            continue
        sql_ok += 1
        note = looks_suspicious(result.rows, result.columns)
        if note:
            suspicious.append((qid, note))

    print(f"\n  {sql_ok} ran, {sql_fail} failed")
    if suspicious:
        print(f"\n  {len(suspicious)} returned a shape worth checking by hand:")
        for qid, note in suspicious:
            print(f"    {qid}  {note}")

    print("\n--- 2. refusal rules agree with the gold set ---")
    for q in questions:
        qid, expected = q["id"], q["expect"]
        verdict = ambiguity.assess(q["question"])
        actual = "refuse" if verdict.should_refuse else "answer"
        if actual == expected:
            class_ok += 1
        else:
            class_fail += 1
            mismatches.append((qid, q["category"], expected, verdict.rule))

    print(f"  {class_ok} agree, {class_fail} disagree")
    if mismatches:
        print("\n  disagreements (a human must resolve each):")
        for qid, category, expected, rule in mismatches:
            got = "answer" if expected == "refuse" else "refuse"
            print(f"    {qid:5s} [{category:12s}] gold says {expected:6s}, "
                  f"rules say {got:6s}  (rule: {rule})")

    print("\n--- 3. ORDER BY detection on gold queries ---")
    ordered = [q["id"] for q in questions
               if q.get("gold_sql") and gold_requires_order(q["gold_sql"])]
    print(f"  {len(ordered)} gold queries constrain row order: {', '.join(ordered)}")

    print("\n--- 4. the designed ambiguities still fire ---")
    trap_fail = verify_traps(db)

    db.close()

    by_category: dict[str, int] = {}
    for q in questions:
        by_category[q["category"]] = by_category.get(q["category"], 0) + 1
    print("\n--- composition ---")
    for category, count in sorted(by_category.items()):
        print(f"  {category:14s} {count:>3}")

    return 1 if (sql_fail or class_fail or trap_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
