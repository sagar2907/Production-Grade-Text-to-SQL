"""Capture the side-by-side demo into the README.

A reviewer spends under a minute on a repo and will not clone it to run
anything. The contrast between the two arms has to be visible on the page,
above the fold, or it does not exist as far as the reader is concerned.

This runs scripts/demo.py, saves the output to docs/demo-output.txt, and
injects it into the README between the DEMO markers. Re-run it whenever the
semantic layer or the model changes, so the transcript on the page is always
one an actual run produced rather than one that was true three commits ago.

    python scripts/capture_demo.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
OUT = ROOT / "docs" / "demo-output.txt"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)

    print("running scripts/demo.py ...")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "demo.py")],
        capture_output=True, text=True, cwd=str(ROOT), timeout=900,
    )
    if proc.returncode != 0:
        print("demo.py failed:")
        print(proc.stderr[-2000:])
        return 1

    # DuckDB prints sqlglot parser warnings to stderr for EXPLAIN; they are
    # noise here and would only confuse a reader of the transcript.
    text = "\n".join(
        line for line in proc.stdout.splitlines()
        if "unsupported syntax" not in line
    ).rstrip()

    OUT.write_text(text + "\n", encoding="utf-8")
    print(f"  saved {len(text.splitlines())} lines to {OUT.relative_to(ROOT)}")

    readme = README.read_text(encoding="utf-8")
    start, end = "<!-- DEMO-START -->", "<!-- DEMO-END -->"
    if start not in readme or end not in readme:
        print("  warning: DEMO markers not found in README; transcript not injected")
        return 0

    block = f"```\n{text}\n```"
    head = readme.split(start)[0]
    tail = readme.split(end)[1]
    README.write_text(f"{head}{start}\n{block}\n{end}{tail}", encoding="utf-8")
    print("  injected into README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
