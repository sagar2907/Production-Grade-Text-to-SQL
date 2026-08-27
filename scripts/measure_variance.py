"""Measure run-to-run variance in the evaluation.

Why this exists: the baseline arm's result moved by one question between two
runs, despite the baseline being untouched by the change under test and the
generator running at temperature 0. Local inference is not bit-reproducible
across server restarts -- GPU batching and KV-cache state differ -- so the
harness has noise, and a headline number quoted without its spread is not a
falsifiable claim.

This runs each arm N times and reports mean, min and max for every headline
metric. The spread it prints is the resolution limit of the whole evaluation:
any claimed improvement smaller than that range is indistinguishable from
noise and must not be reported as an improvement.

    python scripts/measure_variance.py --repeats 3
    python scripts/measure_variance.py --repeats 5 --arm semantic
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta.db import Database                                   # noqa: E402
from rosetta.evaluate import Outcome, load_questions, run          # noqa: E402
from rosetta.generate import OllamaProvider, available_models      # noqa: E402
from rosetta.pipeline import Pipeline                              # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.yaml"
DB = ROOT / "data" / "rosetta.duckdb"
OUT = ROOT / "results" / "variance.json"

METRICS = [
    ("confidently_wrong_rate", "confidently wrong", True),
    ("execution_accuracy", "execution accuracy", False),
    ("refusal_recall", "refusal recall", False),
]


def summarise(values: list[float]) -> dict[str, float]:
    return {
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "spread": max(values) - min(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--arm", choices=["baseline", "semantic", "both"], default="both")
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    args = parser.parse_args()

    if not DB.exists():
        print(f"database missing at {DB}; run scripts/load_data.py first")
        return 1
    if args.model not in available_models():
        print(f"model {args.model!r} not reachable; is `ollama serve` running?")
        return 1

    questions = load_questions(QUESTIONS)
    provider = OllamaProvider(model=args.model)
    arms = ["baseline", "semantic"] if args.arm == "both" else [args.arm]

    print(f"model: {provider.name}   questions: {len(questions)}   "
          f"repeats: {args.repeats}\n")

    collected: dict[str, dict] = {}

    for arm in arms:
        print(f"--- {arm} arm, {args.repeats} runs ---")
        runs: list[dict] = []
        per_question_outcomes: dict[str, list[str]] = {}

        for i in range(1, args.repeats + 1):
            db = Database(DB)
            pipeline = Pipeline(
                db=db, provider=provider, use_semantic_layer=(arm == "semantic")
            )
            report = run(pipeline, questions, progress=False)
            db.close()

            runs.append({
                key: getattr(report, key) for key, _, _ in METRICS
            })
            for r in report.results:
                per_question_outcomes.setdefault(r.id, []).append(r.outcome.value)

            cw = report.confidently_wrong_rate
            ex = report.execution_accuracy
            print(f"  run {i}: confidently wrong {cw:>6.1%}   "
                  f"execution accuracy {ex:>6.1%}   ({report.elapsed_seconds:.0f}s)")

        stats = {key: summarise([r[key] for r in runs]) for key, _, _ in METRICS}
        collected[arm] = {"runs": runs, "stats": stats}

        print()
        for key, label, _ in METRICS:
            s = stats[key]
            print(f"  {label:20s} mean {s['mean']:>6.1%}   "
                  f"range {s['min']:.1%}-{s['max']:.1%}   "
                  f"spread {s['spread']*100:>4.1f} pts")

        # Which questions are actually unstable, as opposed to the aggregate
        # merely wobbling. This is the more useful diagnostic: a stable 90%
        # with 6 flapping questions is a different situation from every
        # question being borderline.
        unstable = {
            qid: outs for qid, outs in per_question_outcomes.items()
            if len(set(outs)) > 1
        }
        print(f"\n  {len(unstable)} of {len(questions)} questions gave a "
              f"different outcome between runs:")
        for qid, outs in sorted(unstable.items()):
            print(f"    {qid:5s} {' -> '.join(outs)}")
        collected[arm]["unstable"] = unstable
        print()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(collected, indent=2), encoding="utf-8")

    print("=" * 70)
    if len(collected) == 2:
        b = collected["baseline"]["stats"]["confidently_wrong_rate"]
        s = collected["semantic"]["stats"]["confidently_wrong_rate"]
        effect = b["mean"] - s["mean"]
        noise = max(b["spread"], s["spread"])
        print(f"  effect of the semantic layer:  {effect*100:>5.1f} pts")
        print(f"  largest single-arm spread:     {noise*100:>5.1f} pts")
        ratio = effect / noise if noise else float("inf")
        print(f"  effect / noise:                {ratio:>5.1f}x")
        if ratio > 5:
            print("\n  The effect is far larger than the harness noise, so the")
            print("  headline difference is real. Improvements SMALLER than the")
            print("  spread above are not distinguishable from noise and should")
            print("  not be reported as improvements.")
    print(f"\n  written to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
