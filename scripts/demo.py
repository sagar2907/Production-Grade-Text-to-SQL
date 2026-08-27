"""The side-by-side demo.

One ambiguous question, run twice against the identical model and the identical
schema. Without the semantic layer it picks a reading silently and returns a
confident number. With the layer it asks which of the certified readings you
meant.

That contrast is the project in fifteen seconds, and it is what belongs at the
top of the README.

    python scripts/demo.py
    python scripts/demo.py --question "How much did the government spend in 2019-20?"
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta.db import Database                                  # noqa: E402
from rosetta.generate import OllamaProvider, default_provider    # noqa: E402
from rosetta.pipeline import AnswerKind, Pipeline                # noqa: E402

DB = ROOT / "data" / "rosetta.duckdb"

# Chosen because it is ambiguous on two axes at once and because the wrong
# answer is not obviously wrong -- it is a plausible budget figure.
DEFAULT_QUESTION = "How much did the government spend in 2019-20?"

WIDTH = 74


def rule(char: str = "-") -> str:
    return char * WIDTH


def show(title: str, body: str) -> None:
    print(f"\n{rule('=')}")
    print(f"  {title}")
    print(rule('='))
    print(body)


def render(answer, db: Database) -> str:
    if answer.kind is AnswerKind.REFUSED:
        return textwrap.indent(answer.refusal, "  ")

    if answer.kind is AnswerKind.FAILED:
        attempts = answer.resolution.attempt_count if answer.resolution else 0
        return f"  Could not produce runnable SQL after {attempts} attempt(s)."

    lines = ["  SQL:"]
    for line in (answer.sql or "").splitlines():
        lines.append(f"    {line}")
    lines.append("")
    lines.append("  Result:")
    for row in answer.rows[:5]:
        rendered = ", ".join(
            f"{v:,.0f}" if isinstance(v, (int, float)) and not isinstance(v, bool)
            else str(v)
            for v in row
        )
        lines.append(f"    {rendered}")
    if len(answer.rows) > 5:
        lines.append(f"    ... {len(answer.rows) - 5} more rows")
    lines.append("")
    lines.append("  No caveat. No question asked. The user has no way to know")
    lines.append("  which of the three estimate bases this is, or whether it")
    lines.append("  includes Interest Payments.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    if not DB.exists():
        print(f"database missing at {DB}; run scripts/load_data.py first")
        return 1

    provider = OllamaProvider(model=args.model) if args.model else default_provider()

    print(rule("="))
    print("  ROSETTA -- the same question, the same model, twice")
    print(rule("="))
    print(f"\n  Question: {args.question}")
    print(f"  Model:    {provider.name}")

    db = Database(DB)

    baseline = Pipeline(db=db, provider=provider, use_semantic_layer=False)
    baseline_answer = baseline.ask(args.question)
    show("WITHOUT the semantic layer", render(baseline_answer, db))

    # Capture the single figure the baseline reported, if it reported one, so
    # the closing note can check it against the certified readings.
    baseline_value = None
    if baseline_answer.rows and len(baseline_answer.rows[0]) == 1:
        candidate = baseline_answer.rows[0][0]
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            baseline_value = float(candidate)

    semantic = Pipeline(db=db, provider=provider, use_semantic_layer=True)
    show("WITH the semantic layer", render(semantic.ask(args.question), db))

    # The figures the two certified readings actually produce, so the size of
    # the mistake is visible rather than asserted.
    print(f"\n{rule('=')}")
    print("  What the certified readings actually return")
    print(rule('='))
    year = "2019-20"
    readings: list[tuple[str, float]] = []
    for label, sql in [
        ("Budget Estimate, all spending",
         f"SELECT SUM(be_amount_crore) FROM budget_allocations WHERE fiscal_year='{year}'"),
        ("Budget Estimate, ministries and departments only",
         "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
         "JOIN ministries m ON a.ministry_id=m.ministry_id "
         f"WHERE a.fiscal_year='{year}' AND m.entity_kind IN ('ministry','department')"),
        ("Revised Estimate, all spending",
         f"SELECT SUM(re_amount_crore) FROM budget_allocations WHERE fiscal_year='{year}'"),
        ("Actuals, all spending",
         f"SELECT SUM(actual_amount_crore) FROM budget_allocations WHERE fiscal_year='{year}'"),
    ]:
        result = db.execute(sql)
        value = result.rows[0][0] if result.ok and result.rows else None
        rendered = f"{value:>16,.0f}" if value is not None else f"{'no data':>16s}"
        print(f"  {label:<50s} {rendered}")
        if value is not None:
            readings.append((label, float(value)))

    # Computed, never hardcoded. A demo for a project about unverified numbers
    # has no business asserting a spread it did not measure.
    if readings:
        low = min(v for _, v in readings)
        high = max(v for _, v in readings)
        spread = high / low if low else 0
        print(f"\n  {len(readings)} defensible answers to one question, "
              f"spanning a {spread:.1f}x range.")
        print("  Picking one silently is the failure this project measures.")

        # If the baseline's figure is not close to ANY certified reading, it
        # did not merely pick the wrong one -- it invented a fifth.
        if baseline_value is not None:
            nearest = min(readings, key=lambda kv: abs(kv[1] - baseline_value))
            drift = abs(nearest[1] - baseline_value) / nearest[1] if nearest[1] else 0
            if drift > 0.05:
                print(f"\n  Note: the baseline answered {baseline_value:,.0f}, which is "
                      f"not any of these.")
                print(f"  Its nearest certified reading is {nearest[1]:,.0f} "
                      f"({nearest[0]}),")
                print(f"  which it is off by {drift:.0%}.")
                print("  It did not pick the wrong reading. It produced a fifth number,")
                print("  with no indication that it had.")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
