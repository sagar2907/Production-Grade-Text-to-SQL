"""The semantic layer.

Loads the versioned YAML in semantic/ and turns it into the things the
pipeline actually needs: a schema description worth putting in a prompt, the
certified reading of a metric, and the full lineage of an entity that has been
renamed.

The thesis of the whole project is that this file, not the model, is what
closes the gap between a text-to-SQL demo and a text-to-SQL system. The
ablation that proves it runs the identical model with this layer switched off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

SEMANTIC_DIR = Path(__file__).resolve().parents[2] / "semantic"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
@dataclass
class SemanticLayer:
    glossary: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    joins: dict[str, Any] = field(default_factory=dict)

    @property
    def dimensions(self) -> dict[str, Any]:
        return self.metrics.get("dimensions", {})

    @property
    def metric_list(self) -> list[dict[str, Any]]:
        return self.metrics.get("metrics", [])

    @property
    def columns(self) -> dict[str, Any]:
        return self.glossary.get("columns", {})

    def metric(self, metric_id: str) -> dict[str, Any] | None:
        for m in self.metric_list:
            if m.get("id") == metric_id:
                return m
        return None


@lru_cache(maxsize=1)
def load_semantic_layer(directory: str | None = None) -> SemanticLayer:
    base = Path(directory) if directory else SEMANTIC_DIR

    def read(name: str) -> dict[str, Any]:
        path = base / name
        if not path.exists():
            raise FileNotFoundError(f"semantic layer file missing: {path}")
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    return SemanticLayer(
        glossary=read("glossary.yaml"),
        metrics=read("metrics.yaml"),
        joins=read("joins.yaml"),
    )


# ---------------------------------------------------------------------------
# Entity lineage
# ---------------------------------------------------------------------------
def resolve_lineage(db, entity_name: str) -> list[int]:
    """Every ministry_id that is the same money as `entity_name`.

    An entity renamed or merged mid-series has more than one id. Filtering on
    a single name returns a truncated series, and a truncated series looks
    exactly like a spending cliff -- which is the failure this function
    exists to prevent.

    Walks both directions: predecessors via renamed_from_id, and entities that
    were merged into this one via merged_into_id.
    """
    row = db.execute(
        "SELECT ministry_id FROM ministries WHERE lower(ministry_name) = lower("
        f"{_quote(entity_name)})"
    )
    if not row.ok or not row.rows:
        return []

    seen: set[int] = set()
    frontier = [int(r[0]) for r in row.rows]

    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)

        # Predecessors of the current entity.
        pred = db.execute(
            "SELECT renamed_from_id FROM ministries "
            f"WHERE ministry_id = {current} AND renamed_from_id IS NOT NULL"
        )
        if pred.ok:
            frontier.extend(int(r[0]) for r in pred.rows if r[0] is not None)

        # Entities that were merged into the current one.
        merged = db.execute(
            f"SELECT ministry_id FROM ministries WHERE merged_into_id = {current}"
        )
        if merged.ok:
            frontier.extend(int(r[0]) for r in merged.rows)

        # Successors, so the lineage resolves the same set from either end.
        succ = db.execute(
            f"SELECT ministry_id FROM ministries WHERE renamed_from_id = {current}"
        )
        if succ.ok:
            frontier.extend(int(r[0]) for r in succ.rows)

        onward = db.execute(
            "SELECT merged_into_id FROM ministries "
            f"WHERE ministry_id = {current} AND merged_into_id IS NOT NULL"
        )
        if onward.ok:
            frontier.extend(int(r[0]) for r in onward.rows)

    return sorted(seen)


def lineage_names(db, entity_name: str) -> list[tuple[int, str, int, int | None]]:
    ids = resolve_lineage(db, entity_name)
    if not ids:
        return []
    id_list = ", ".join(str(i) for i in ids)
    result = db.execute(
        "SELECT ministry_id, ministry_name, effective_from_year, effective_to_year "
        f"FROM ministries WHERE ministry_id IN ({id_list}) ORDER BY effective_from_year"
    )
    return [(int(r[0]), r[1], r[2], r[3]) for r in result.rows] if result.ok else []


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
def schema_description(db, *, max_columns_per_table: int | None = None) -> str:
    """A plain description of the schema, for the prompt.

    Used by BOTH arms of the ablation. The baseline arm gets this and nothing
    else; the semantic arm gets this plus semantic_context(). Keeping the
    schema identical between arms is what makes the ablation measure the
    semantic layer rather than the prompt length.
    """
    lines: list[str] = []
    for table in db.list_tables():
        cols = db.describe_table(table)
        if max_columns_per_table:
            shown = cols[:max_columns_per_table]
            suffix = f"  -- +{len(cols) - len(shown)} more" if len(cols) > len(shown) else ""
        else:
            shown, suffix = cols, ""
        rendered = ", ".join(f"{name} {dtype}" for name, dtype in shown)
        lines.append(f"{table}({rendered}){suffix}")
    return "\n".join(lines)


# Low-cardinality columns whose exact string values a generator has to know.
# Guessing them produces a query that runs, matches nothing, and returns a
# clean empty result or a NULL sum -- which reads as a real answer.
ENUM_COLUMNS = [
    ("ministries", "entity_kind"),
    ("schemes", "status"),
    ("schemes", "scheme_type"),
    ("account_heads", "account_type"),
    ("receipts", "receipt_type"),
    ("transfers_to_states", "transfer_type"),
    ("demands", "demand_type"),
]

# Columns where NULL carries meaning rather than absence. Getting these wrong
# is worse than an error: `effective_to_year >= 2016` silently drops every
# still-active entity, because NULL >= 2016 is NULL, not TRUE.
NULL_SEMANTICS = [
    ("ministries.effective_to_year",
     "NULL means the entity is STILL ACTIVE. To test whether an entity existed "
     "in year Y, write (effective_to_year IS NULL OR effective_to_year >= Y). "
     "Writing effective_to_year >= Y alone drops every active entity."),
    ("schemes.closure_year",
     "NULL means the scheme is still running. Same pattern as above."),
    ("budget_allocations.state_code",
     "NULL means the allocation is not a state transfer -- true of most rows. "
     "An INNER JOIN to states therefore drops most of the budget; use LEFT."),
    ("budget_allocations.actual_amount_crore",
     "NULL for the two most recent years because actuals are not published "
     "yet. SUM() skips NULLs silently, so a total over those years returns a "
     "smaller number rather than an error."),
]


def value_domains(db) -> str:
    """The exact permitted values for low-cardinality columns.

    Read from the database rather than hardcoded, so this cannot drift out of
    date when the generator changes. A generator that guesses 'Closed' when
    the data says 'closed' produces a query that runs and matches nothing.
    """
    lines: list[str] = []
    for table, column in ENUM_COLUMNS:
        result = db.execute(
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} IS NOT NULL ORDER BY 1"
        )
        if not result.ok or not result.rows:
            continue
        values = ", ".join(f"'{r[0]}'" for r in result.rows)
        lines.append(f"  {table}.{column} is exactly one of: {values}")
    return "\n".join(lines)


def semantic_context(layer: SemanticLayer, *, question: str = "", db=None) -> str:
    """The semantic layer, rendered for a prompt.

    Only the parts that bear on ambiguity are included. Dumping the whole
    glossary would make the semantic arm win on context length rather than on
    content, which would make the ablation meaningless.
    """
    parts: list[str] = []

    parts.append("UNIT RULES")
    parts.append(
        "  All amounts are in rupees crore. Columns ending _lakh are exactly 100x\n"
        "  the crore column and columns ending _rupees are 10,000,000x. Never sum\n"
        "  a _lakh or _rupees column when reporting a total; use the _crore column."
    )

    parts.append("\nESTIMATE BASIS")
    parts.append(
        "  be_amount_crore    = Budget Estimate, available for every year\n"
        "  re_amount_crore    = Revised Estimate, NULL for the most recent year\n"
        "  actual_amount_crore= Actuals, NULL for the two most recent years\n"
        "  These are three different numbers. Do not substitute one for another."
    )

    parts.append("\nENTITY SCOPE")
    parts.append(
        "  ministries.entity_kind is 'ministry', 'department' or 'functional'.\n"
        "  Functional rows (Interest Payments, Defence Pensions, Transfers to\n"
        "  States) are real spending but are not administrative bodies and are\n"
        "  ~44% of the total. 'Total expenditure' includes them; 'what ministries\n"
        "  spent' does not."
    )

    parts.append("\nERA BOUNDARY")
    parts.append(
        "  plan_amount_crore and non_plan_amount_crore are populated ONLY for\n"
        "  2014-15 to 2016-17. scheme_amount_crore and establishment_amount_crore\n"
        "  ONLY from 2017-18. Summing across the boundary silently drops years."
    )

    parts.append("\nWHERE ENTITIES LIVE")
    parts.append(
        "  The `ministries` table holds EVERY spending entity, including ones\n"
        "  named 'Department of ...' and functional lines like 'Interest\n"
        "  Payments'. To find spending by a named entity, match\n"
        "  ministries.ministry_name -- NOT the `departments` table, whose\n"
        "  department_name holds internal sub-units ('Secretariat, MoR') and\n"
        "  never the entity name a question will use."
    )

    if db is not None:
        domains = value_domains(db)
        if domains:
            parts.append("\nEXACT COLUMN VALUES (do not guess these)")
            parts.append(domains)

    parts.append("\nNULL MEANS SOMETHING HERE")
    for column, meaning in NULL_SEMANTICS:
        wrapped = meaning.replace(". ", ".\n    ")
        parts.append(f"  {column}:\n    {wrapped}")

    parts.append("\nGROSS vs NET")
    parts.append(
        "  gross_amount_crore is expenditure BEFORE recoveries; net_amount_crore\n"
        "  is after. Both already exist as columns -- never compute one from the\n"
        "  other. 'Gross' or 'before recoveries' means gross_amount_crore."
    )

    parts.append("\nRECEIPTS")
    parts.append(
        "  receipts.receipt_type is exactly one of: 'tax', 'non_tax', 'capital'.\n"
        "  There is no 'Revenue' value -- use is_revenue_receipt = TRUE instead.\n"
        "  Borrowings are capital receipts: is_borrowing = TRUE. Including them\n"
        "  roughly doubles any receipts total.\n"
        "  net_to_centre_crore is gross tax revenue minus the states' devolution\n"
        "  share (~41%). Use that column; do not subtract devolution by hand."
    )

    parts.append("\nJOIN RULES")
    for join in layer.joins.get("forbidden", []):
        parts.append(f"  FORBIDDEN: {join.get('id')} -- {_first_line(join.get('why_forbidden',''))}")
    for join in layer.joins.get("ambiguous", []):
        if join.get("resolution"):
            parts.append(f"  RESOLVE:   {join.get('id')} -- {_first_line(join['resolution'])}")

    parts.append("\nENTITY RENAMES (same money, different name)")
    for item in _renames(layer):
        parts.append(f"  {item}")

    return "\n".join(parts)


def _renames(layer: SemanticLayer) -> list[str]:
    for join in layer.joins.get("ambiguous", []):
        if join.get("id") == "entity_across_a_rename":
            return [str(x) for x in join.get("affected", [])]
    return []


def _first_line(text: str, limit: int = 150) -> str:
    flat = " ".join(str(text).split())
    return flat[:limit] + ("..." if len(flat) > limit else "")
