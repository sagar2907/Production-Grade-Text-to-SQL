"""Render docs/REPORT.md to PDF, then verify the PDF by looking at it.

The source of truth is the markdown, so the report stays reviewable in the
repository and shows up in diffs. This script only renders it.

The verification step matters more than it sounds. A missing font glyph does
not raise an error -- it renders as a black box, an empty rectangle, or
nothing at all, and a PDF full of them still passes a text-extraction check
because the text layer is intact. So this rasterises every page and inspects
the pixels: pages that are blank, pages that are suspiciously dark, and pages
whose extracted text does not match their ink coverage all get flagged.

    python scripts/build_report.py
    python scripts/build_report.py --out "C:/somewhere/Report.pdf"
    python scripts/build_report.py --keep-images     # leave the page PNGs
"""

from __future__ import annotations

import argparse
import datetime as _dt
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "REPORT.md"
BUILD = ROOT / "docs" / "_build"
# Inside the repo, so the folder always carries its own report. An absolute
# path lived here once; it wrote the PDF to a machine-specific directory and
# left a clone with no report at all.
DEFAULT_OUT = ROOT / "docs" / "Rosetta_Project_Report.pdf"

BROWSERS = [
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
]

# Only web-safe families are named. A font that is not installed falls back
# silently, and the fallback is what produces missing glyphs -- so the stack
# is deliberately boring.
CSS = """
@page { size: A4; margin: 16mm 15mm 16mm 15mm; }
* { box-sizing: border-box; }
body {
  font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  font-size: 10pt; line-height: 1.5; color: #16191d; margin: 0;
}
h1 { font-size: 21pt; margin: 0 0 8px; letter-spacing: -0.5px; }
h1 + p strong { font-size: 11pt; }
h2 {
  font-size: 13.5pt; margin: 22px 0 9px; padding: 6px 0 4px;
  border-top: 2px solid #16191d; border-bottom: 0.5px solid #c3ccd6;
  letter-spacing: -0.2px; page-break-after: avoid;
}
h3 { font-size: 11pt; margin: 15px 0 5px; color: #14304d; page-break-after: avoid; }
p { margin: 0 0 8px; }
ul, ol { margin: 0 0 9px; padding-left: 20px; }
li { margin-bottom: 3px; }
table { width: 100%; border-collapse: collapse; margin: 9px 0 13px; table-layout: fixed; }
th {
  text-align: left; font-size: 8pt; text-transform: uppercase; letter-spacing: 0.5px;
  color: #46505e; background: #eef1f4; border-bottom: 1px solid #9aa4b0; padding: 5px 6px;
}
td {
  padding: 4px 6px; border-bottom: 0.5px solid #dde2e7; vertical-align: top;
  font-size: 9pt; word-wrap: break-word; overflow-wrap: break-word;
}
tr { page-break-inside: avoid; }
code {
  font-family: Consolas, "Courier New", monospace; font-size: 8.6pt;
  background: #eef1f4; padding: 0 3px;
}
pre {
  font-family: Consolas, "Courier New", monospace; font-size: 8.4pt;
  background: #f5f7f9; border-left: 3px solid #9aa4b0; padding: 9px 11px;
  margin: 9px 0 13px; white-space: pre-wrap; line-height: 1.42;
  page-break-inside: avoid;
}
pre code { background: none; padding: 0; font-size: inherit; }
blockquote {
  margin: 10px 0 13px; padding: 9px 13px; background: #f5f7f9;
  border-left: 3px solid #14304d; page-break-inside: avoid;
}
blockquote p { margin: 0 0 6px; }
blockquote p:last-child { margin-bottom: 0; }
hr { border: none; border-top: 1px solid #c3ccd6; margin: 20px 0; }
em { color: #16191d; }
a { color: #14304d; text-decoration: none; }
"""


def find_browser() -> Path:
    for b in BROWSERS:
        if b.exists():
            return b
    raise SystemExit(
        "No Chrome or Edge found. Checked:\n  " + "\n  ".join(str(b) for b in BROWSERS)
    )


def git(*args: str) -> str:
    """Read something out of git, or return "" if this is not a checkout."""
    try:
        done = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def stamp_revision(text: str) -> str:
    """Fill in the commit and date placeholders at build time.

    The header used to carry a hardcoded short hash. History was later
    rewritten, the hash stopped resolving, and the report confidently cited a
    commit that did not exist -- the same drift the report's own Shape C is
    about. Deriving it here means it cannot go stale.
    """
    commit = git("rev-parse", "--short", "HEAD") or "working tree"
    date = git("log", "-1", "--format=%ad", "--date=format:%d %B %Y")
    if not date:
        date = _dt.date.today().strftime("%d %B %Y")
    if git("status", "--porcelain"):
        commit += " + uncommitted changes"
    return text.replace("{{COMMIT}}", commit).replace("{{DATE}}", date)


def stamp_footer(pdf: Path) -> None:
    """Draw a page number into the bottom margin of every page.

    Chrome's --print-to-pdf has no custom-footer option, so this is done
    afterwards. The @page rule leaves 16mm at the foot to write into.
    """
    try:
        import pymupdf
    except ImportError:
        return
    doc = pymupdf.open(pdf)
    grey = (0.54, 0.56, 0.63)
    for number, page in enumerate(doc, start=1):
        label = str(number)
        page.insert_text(
            (page.rect.width - 42 - pymupdf.get_text_length(label, "helv", 7.5),
             page.rect.height - 26),
            label, fontname="helv", fontsize=7.5, color=grey,
        )
        page.insert_text(
            (42, page.rect.height - 26),
            "Rosetta · project report", fontname="helv", fontsize=7.5, color=grey,
        )
    doc.saveIncr()
    doc.close()


def render_html() -> Path:
    try:
        import markdown
    except ImportError:
        raise SystemExit("pip install markdown")

    text = stamp_revision(SOURCE.read_text(encoding="utf-8"))
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
    )
    BUILD.mkdir(parents=True, exist_ok=True)
    html = BUILD / "report.html"
    html.write_text(
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        "<title>Rosetta - Project Report</title>"
        f"<style>{CSS}</style></head><body>\n{body}\n</body></html>",
        encoding="utf-8",
    )
    return html


def render_pdf(html: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    browser = find_browser()
    url = "file:///" + str(html.resolve()).replace("\\", "/")
    subprocess.run(
        [
            str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-pdf-header-footer", "--virtual-time-budget=6000",
            f"--print-to-pdf={out}", url,
        ],
        check=True, capture_output=True, timeout=180,
    )


def verify(pdf: Path, keep_images: bool) -> int:
    """Rasterise every page and inspect it. Returns the number of problems."""
    try:
        import pymupdf
    except ImportError:
        print("  pymupdf not installed - CANNOT verify visually. pip install pymupdf")
        return 1

    doc = pymupdf.open(pdf)
    images = BUILD / "pages"
    images.mkdir(parents=True, exist_ok=True)

    problems = 0
    print(f"  {doc.page_count} pages\n")
    print(f"  {'page':>5} {'ink %':>7} {'chars':>7}  status")
    print(f"  {'-'*5} {'-'*7} {'-'*7}  {'-'*40}")

    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=110)
        png = images / f"page_{i:02d}.png"
        pix.save(png)

        # Ink coverage: share of pixels that are not near-white. A page of
        # black boxes shows up as a large dark fraction; a blank page as ~0.
        from PIL import Image
        im = Image.open(png).convert("L")
        px = list(im.getdata())
        total = len(px)
        dark = sum(1 for v in px if v < 200)
        very_dark = sum(1 for v in px if v < 60)
        ink = dark / total * 100
        heavy = very_dark / total * 100

        chars = len(page.get_text().strip())

        notes = []
        if chars == 0 and ink < 0.5:
            notes.append("BLANK PAGE")
        if heavy > 12:
            notes.append(f"very dark ({heavy:.1f}% near-black) - possible glyph boxes")
        if chars > 200 and ink < 0.6:
            notes.append("text present but almost no ink - font may not be rendering")
        if chars < 40 and ink > 3:
            notes.append("ink without text - image-only or broken text layer")

        if notes:
            problems += 1
        status = "; ".join(notes) if notes else "ok"
        print(f"  {i:>5} {ink:>6.2f}% {chars:>7}  {status}")

    doc.close()

    if not keep_images:
        # Leave them on failure so they can be inspected.
        if problems == 0:
            shutil.rmtree(images, ignore_errors=True)
        else:
            print(f"\n  page images kept at {images.relative_to(ROOT)} for inspection")
    else:
        print(f"\n  page images at {images.relative_to(ROOT)}")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--keep-images", action="store_true")
    args = ap.parse_args()

    if not SOURCE.exists():
        print(f"source missing: {SOURCE}")
        return 1

    out = Path(args.out)
    words = len(SOURCE.read_text(encoding="utf-8").split())
    print(f"source: {SOURCE.relative_to(ROOT)}  ({words:,} words)")

    html = render_html()
    print(f"html:   {html.relative_to(ROOT)}")

    render_pdf(html, out)
    if not out.exists() or out.stat().st_size == 0:
        print(f"FAILED: {out} missing or empty")
        return 1
    stamp_footer(out)
    print(f"pdf:    {out}  ({out.stat().st_size:,} bytes)\n")

    print("visual verification")
    problems = verify(out, args.keep_images)

    print()
    if problems:
        print(f"  {problems} page(s) flagged - inspect before using this PDF")
        return 1
    print("  all pages render cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
