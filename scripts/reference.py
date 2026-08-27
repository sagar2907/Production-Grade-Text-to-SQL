"""Read the real Union Budget reference data.

Source: `Government Budget DATA.csv`, extracted from
https://github.com/aryanshenoy/Indian-Union-Budget-Analysis, originally sourced
from Open Budgets India. 103 rows covering Actuals 2022-23, BE and RE 2023-24,
and BE 2024-25, in rupees crore.

Note that openbudgetsindia.org itself is dead as of August 2026 -- the domain
now serves an unrelated site -- so this GitHub copy is the surviving artefact.
Worth saying out loud in the README, because a reviewer who tries the obvious
source will find a casino.

Everything downstream is anchored to these figures: entity names are real,
magnitudes are real, and the relationship between BE, RE and Actuals is taken
from the file rather than drawn from a distribution. What is synthetic is the
*detail* -- the scheme, head and month level breakdown that the published
entity-level file does not contain.

Three properties of the real file are preserved deliberately, because each one
is a failure mode worth measuring:

  1. GRAND TOTAL IS A ROW. The file lists 'Grand Total' alongside the
     components that make it up. SUM() over the column double-counts and
     returns roughly twice the true figure -- cleanly, plausibly, and wrongly.

  2. MIXED GRANULARITY. 'Particulars' contains ministries ('Ministry of
     Railways'), departments ('Department of Fertilisers') and functional
     heads ('Interest Payments', 'Defence Pensions'). They are not the same
     kind of entity and cannot be summed as though they were.

  3. '...' MEANS NIL. Four cells hold a dotted placeholder rather than a
     number. Coercing that to zero is usually right and occasionally wrong;
     either way the eval should know which cells they are.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_CSV = (
    ROOT / "data" / "raw" / "Indian-Union-Budget-Analysis-main"
    / "Project" / "assets" / "Government Budget DATA.csv"
)

# Rows that are totals or subtotals rather than spending entities. Kept in the
# reference table (they are real, and the double-counting trap depends on them
# being reachable) but excluded from the entity list the fact table is built
# from, so the generated detail does not itself double-count.
AGGREGATE_ROWS = {
    "grand total",
    "total",
}

# Rows that are functional heads rather than administrative entities. Real, and
# deliberately kept, but flagged so a question can legitimately ask for
# "ministries" and exclude them.
FUNCTIONAL_HEADS = {
    "interest payments",
    "transfers to states",
    "defence pensions",
    "defence services (revenue)",
    "capital outlay on defence services",
    "defence services (capital)",
    "pensions",
    "subsidies",
}

COLUMN_MAP = {
    "Actuals 2022-2023": ("2022-23", "actual"),
    "Budget Estimates 2023-2024 Total": ("2023-24", "be"),
    "Revised Estimates 2023-2024": ("2023-24", "re"),
    "Budget Estimates 2024-2025 Total": ("2024-25", "be"),
}


@dataclass(frozen=True)
class ReferenceEntity:
    name: str
    kind: str                    # 'ministry' | 'department' | 'functional' | 'aggregate'
    actual_2022_23: float | None
    be_2023_24: float | None
    re_2023_24: float | None
    be_2024_25: float | None

    @property
    def anchor(self) -> float:
        """The figure used to scale generated detail for this entity."""
        for value in (self.be_2024_25, self.be_2023_24, self.actual_2022_23):
            if value:
                return value
        return 100.0

    @property
    def is_spending_entity(self) -> bool:
        # Functional heads are included: they are real spending lines and
        # together they are ~43% of the budget, so excluding them stops the
        # database reconciling against the published grand total. They are
        # tagged rather than dropped, so a question can still ask for
        # administrative ministries alone.
        return self.kind in ("ministry", "department", "functional")


def _parse_amount(raw: str | None) -> float | None:
    """Parse a crore figure, returning None for the '...' nil placeholder."""
    if raw is None:
        return None
    text = raw.strip().replace(",", "")
    if not text or set(text) <= {"."} or text in {"-", "NA", "N.A."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _classify(name: str) -> str:
    lowered = name.strip().lower()
    if lowered in AGGREGATE_ROWS:
        return "aggregate"
    if lowered in FUNCTIONAL_HEADS:
        return "functional"
    if lowered.startswith("department"):
        return "department"
    if lowered.startswith("ministry"):
        return "ministry"
    # Secretariats, commissions, 'Atomic Energy', 'Lok Sabha' and similar are
    # administrative spending units even though they are not named as
    # ministries or departments.
    return "department"


def load_reference() -> list[ReferenceEntity]:
    if not REFERENCE_CSV.exists():
        raise FileNotFoundError(
            f"reference data missing at {REFERENCE_CSV}\n"
            "Run scripts/fetch_reference.py to download it."
        )
    entities: list[ReferenceEntity] = []
    with REFERENCE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("Particulars") or "").strip()
            name = re.sub(r"\s+", " ", name)
            if not name:
                continue
            entities.append(
                ReferenceEntity(
                    name=name,
                    kind=_classify(name),
                    actual_2022_23=_parse_amount(row.get("Actuals 2022-2023")),
                    be_2023_24=_parse_amount(row.get("Budget Estimates 2023-2024 Total")),
                    re_2023_24=_parse_amount(row.get("Revised Estimates 2023-2024")),
                    be_2024_25=_parse_amount(row.get("Budget Estimates 2024-2025 Total")),
                )
            )
    return entities


def spending_entities() -> list[ReferenceEntity]:
    """Real entities that actually spend, excluding totals."""
    return [e for e in load_reference() if e.is_spending_entity]


def grand_total() -> float | None:
    for entity in load_reference():
        if entity.name.strip().lower() == "grand total":
            return entity.be_2024_25
    return None


def re_to_be_ratio() -> float:
    """Median RE/BE ratio from the real 2023-24 columns.

    Used to scale generated Revised Estimates rather than inventing a spread,
    so the BE-to-RE relationship in the database reflects how these two figures
    actually diverge.
    """
    ratios = [
        e.re_2023_24 / e.be_2023_24
        for e in load_reference()
        if e.be_2023_24 and e.re_2023_24 and e.be_2023_24 > 0
    ]
    if not ratios:
        return 1.0
    ratios.sort()
    return ratios[len(ratios) // 2]


if __name__ == "__main__":
    rows = load_reference()
    by_kind: dict[str, int] = {}
    for entity in rows:
        by_kind[entity.kind] = by_kind.get(entity.kind, 0) + 1
    print(f"{len(rows)} reference rows")
    for kind, count in sorted(by_kind.items()):
        print(f"  {kind:12s} {count:>4}")
    total = grand_total()
    components = sum(
        e.be_2024_25 or 0 for e in rows if e.kind in ("ministry", "department", "functional")
    )
    print(f"\n  Grand Total row      {total:>14,.0f} crore")
    print(f"  Sum of components    {components:>14,.0f} crore")
    print(f"  Naive SUM(all rows)  {sum(e.be_2024_25 or 0 for e in rows):>14,.0f} crore  <- double-counts")
    print(f"\n  median RE/BE ratio   {re_to_be_ratio():.4f}")
