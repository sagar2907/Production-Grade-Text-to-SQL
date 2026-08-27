"""Run the evaluation.

Both arms by default, which is the whole point: the same model against the same
schema with the semantic layer switched on and off. The difference between the
two confidently-wrong rates is what this project is for.

    python scripts/run_eval.py                    # both arms, default model
    python scripts/run_eval.py --arm semantic     # one arm
    python scripts/run_eval.py --model llama3.1:8b
    python scripts/run_eval.py --limit 6          # smoke test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta.db import Database                                      # noqa: E402
from rosetta.evaluate import (                                       # noqa: E402
    compare_arms, format_summary, load_questions, run, write_report,
)
from rosetta.generate import OllamaProvider, available_models, default_provider  # noqa: E402
from rosetta.pipeline import Pipeline                                # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.yaml"
DB = ROOT / "data" / "rosetta.duckdb"
RESULTS = ROOT / "results"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=["baseline", "semantic", "both"], default="both")
    parser.add_argument("--model", default=None, help="ollama model tag")
    parser.add_argument("--limit", type=int, default=None, help="first N questions only")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if not DB.exists():
        print(f"database missing at {DB}; run scripts/load_data.py first")
        return 1

    installed = available_models()
    if not installed:
        print("No Ollama models reachable at 127.0.0.1:11434.")
        print("Start the server with `ollama serve`, then `ollama pull qwen2.5-coder:7b`.")
        return 1

    provider = OllamaProvider(model=args.model) if args.model else default_provider()
    if args.model and args.model not in installed:
        print(f"model {args.model!r} is not installed. Available: {', '.join(installed)}")
        return 1

    questions = load_questions(QUESTIONS)
    if args.limit:
        # Take a slice that still spans all four categories, so a smoke test
        # exercises refusal as well as answering.
        by_cat: dict[str, list] = {}
        for q in questions:
            by_cat.setdefault(q["category"], []).append(q)
        per = max(1, args.limit // len(by_cat))
        questions = [q for cat in by_cat.values() for q in cat[:per]][: args.limit]

    print(f"model: {provider.name}")
    print(f"questions: {len(questions)}")
    print()

    arms = ["baseline", "semantic"] if args.arm == "both" else [args.arm]
    reports = {}

    for arm in arms:
        print(f"--- {arm} arm ---")
        db = Database(DB)
        pipeline = Pipeline(
            db=db, provider=provider, use_semantic_layer=(arm == "semantic")
        )
        report = run(pipeline, questions, progress=not args.quiet)
        db.close()

        reports[arm] = report
        print()
        print(format_summary(report))
        print()
        out = RESULTS / f"{arm}_{provider.model.replace(':', '-')}.json"
        write_report(report, out)
        print(f"  written to {out.relative_to(ROOT)}")
        print()

    if len(reports) == 2:
        print(compare_arms(reports["baseline"], reports["semantic"]))
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
