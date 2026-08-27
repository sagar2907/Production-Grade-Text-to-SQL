"""Write the measured results into the README.

The README quotes numbers. Hand-transcribed numbers drift from the runs that
produced them, and a project whose entire subject is confidently wrong figures
cannot afford a stale one in its own front matter. So the figures are injected
between markers, straight from the result JSON.

    python scripts/update_readme.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
RESULTS = ROOT / "results"


def load(arm: str) -> dict | None:
    matches = sorted(RESULTS.glob(f"{arm}_*.json"))
    if not matches:
        return None
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def replace_block(text: str, marker: str, body: str) -> str:
    start, end = f"<!-- {marker}-START -->", f"<!-- {marker}-END -->"
    if start not in text or end not in text:
        print(f"  warning: {marker} markers not found in README")
        return text
    head = text.split(start)[0]
    tail = text.split(end)[1]
    return f"{head}{start}\n{body}\n{end}{tail}"


def headline_block(baseline: dict, semantic: dict) -> str:
    cw_b = baseline["headline"]["confidently_wrong_rate"]
    cw_s = semantic["headline"]["confidently_wrong_rate"]
    return (
        f"> **Confidently wrong answers fell from {cw_b:.0%} to {cw_s:.0%}** "
        f"on a {semantic['total']}-question evaluation set — identical model, "
        f"identical schema, semantic layer switched off and on.\n>\n"
        f"> Measured with `{semantic['model']}`. "
        f"{len([r for r in semantic['results'] if r['expect'] == 'refuse'])} of the "
        f"{semantic['total']} questions have no single certified answer and should "
        f"be refused rather than answered."
    )


def ablation_block(baseline: dict, semantic: dict) -> str:
    rows = [
        ("confidently wrong", "confidently_wrong_rate", True),
        ("execution accuracy", "execution_accuracy", False),
        ("refusal recall", "refusal_recall", False),
        ("refusal precision", "refusal_precision", False),
    ]
    lines = [
        f"Model: `{semantic['model']}` · n = {semantic['total']} · "
        f"{baseline['elapsed_seconds'] + semantic['elapsed_seconds']:.0f}s total",
        "",
        "| metric | baseline | semantic | delta |",
        "|---|---:|---:|---:|",
    ]
    for label, key, lower_better in rows:
        b, s = baseline["headline"][key], semantic["headline"][key]
        # Rates are stored as fractions; a delta between them is expressed in
        # PERCENTAGE POINTS, so it has to be scaled. Without the x100 this
        # printed "-0.6 pts" for a 58-point fall.
        delta_points = (s - b) * 100
        arrow = "" if delta_points == 0 else (" ✓" if (delta_points < 0) == lower_better else "")
        lines.append(
            f"| {label} | {b:.1%} | **{s:.1%}** | {delta_points:+.1f} pts{arrow} |"
        )

    lines += ["", "Outcome counts:", "", "| outcome | baseline | semantic |", "|---|---:|---:|"]
    for key in ("correct", "confidently_wrong", "wrong_order", "errored",
                "refused_rightly", "refused_wrongly"):
        b = baseline["counts"].get(key, 0)
        s = semantic["counts"].get(key, 0)
        if b or s:
            lines.append(f"| `{key}` | {b} | {s} |")

    traps_b, traps_s = baseline.get("by_trap", {}), semantic.get("by_trap", {})
    if traps_s:
        lines += ["", "Per trap (correct / total):", "",
                  "| trap | baseline | semantic |", "|---|---:|---:|"]
        for trap in sorted(traps_s):
            b = traps_b.get(trap, {"correct": 0, "total": 0})
            s = traps_s[trap]
            lines.append(
                f"| {trap} | {b['correct']}/{b['total']} | {s['correct']}/{s['total']} |"
            )

    lines += [
        "",
        "`wrong_order` is tracked separately from `confidently_wrong`: right values "
        "in the wrong sequence has not misled anyone about a figure, and folding it "
        "into the headline would inflate the result in this project's favour.",
    ]
    return "\n".join(lines)


def main() -> int:
    baseline, semantic = load("baseline"), load("semantic")
    if not baseline or not semantic:
        print("need both arms in results/; run scripts/run_eval.py first")
        return 1

    text = README.read_text(encoding="utf-8")
    text = replace_block(text, "RESULTS", headline_block(baseline, semantic))
    text = replace_block(text, "ABLATION", ablation_block(baseline, semantic))
    README.write_text(text, encoding="utf-8")

    cw_b = baseline["headline"]["confidently_wrong_rate"]
    cw_s = semantic["headline"]["confidently_wrong_rate"]
    print(f"README updated: confidently wrong {cw_b:.1%} -> {cw_s:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
