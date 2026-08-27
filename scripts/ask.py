"""Ask Rosetta a question.

    python scripts/ask.py "What was the Budget Estimate for the Ministry of Railways in 2019-20?"
    python scripts/ask.py --baseline "How much did the government spend in 2019-20?"
    python scripts/ask.py                # interactive

The SQL is always shown. That is not a debugging convenience -- it is the only
way the person reading the answer can check which reading they got.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta.db import Database                                # noqa: E402
from rosetta.generate import OllamaProvider, default_provider  # noqa: E402
from rosetta.pipeline import AnswerKind, Pipeline              # noqa: E402

DB = ROOT / "data" / "rosetta.duckdb"


def render(answer) -> None:
    if answer.kind is AnswerKind.REFUSED:
        print()
        for line in answer.refusal.splitlines():
            print(f"  {line}")
        print(f"\n  [rule: {answer.refusal_rule}]")
        return

    if answer.kind is AnswerKind.FAILED:
        attempts = answer.resolution.attempt_count if answer.resolution else 0
        print(f"\n  Could not produce runnable SQL after {attempts} attempt(s).")
        if answer.resolution and answer.resolution.attempts:
            last = answer.resolution.attempts[-1]
            print(f"  Last error: {(last.error or '')[:160]}")
            if last.sql:
                print(f"  Last SQL:   {last.sql.replace(chr(10), ' ')[:160]}")
        return

    print("\n  SQL:")
    for line in (answer.sql or "").splitlines():
        print(f"    {line}")

    result = answer.result
    print(f"\n  {len(answer.rows)} row(s):")
    if result and result.columns:
        print("    " + " | ".join(result.columns))
    for row in answer.rows[:20]:
        rendered = " | ".join(
            f"{v:,.2f}" if isinstance(v, float)
            else f"{v:,}" if isinstance(v, int) and not isinstance(v, bool)
            else "NULL" if v is None
            else str(v)
            for v in row
        )
        print(f"    {rendered}")
    if len(answer.rows) > 20:
        print(f"    ... {len(answer.rows) - 20} more")

    if answer.resolution and answer.resolution.needed_repair:
        n = answer.resolution.attempt_count
        print(f"\n  [repaired: first attempt failed, succeeded on attempt {n}]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="*", help="the question; omit for interactive")
    parser.add_argument("--baseline", action="store_true",
                        help="turn the semantic layer OFF")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    if not DB.exists():
        print(f"database missing at {DB}; run scripts/load_data.py first")
        return 1

    provider = OllamaProvider(model=args.model) if args.model else default_provider()
    db = Database(DB)
    pipe = Pipeline(db=db, provider=provider, use_semantic_layer=not args.baseline)

    print(f"model: {provider.name}   arm: {pipe.arm}")

    def answer_one(text: str) -> None:
        started = time.perf_counter()
        result = pipe.ask(text)
        render(result)
        print(f"\n  ({time.perf_counter() - started:.1f}s)")

    if args.question:
        answer_one(" ".join(args.question))
        db.close()
        return 0

    print("Enter a question, or a blank line to quit.\n")
    try:
        while True:
            text = input("> ").strip()
            if not text:
                break
            answer_one(text)
            print()
    except (EOFError, KeyboardInterrupt):
        print()
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
