"""Result-set comparison.

This decides whether a generated query "got the right answer", which makes it
the module every headline number in this project depends on. It is also the
module most likely to be quietly wrong, because SQL result sets differ in a
dozen boring ways that have nothing to do with correctness: column order, row
order, ints that came back as floats, NULL vs NaN, trailing whitespace.

Get this wrong in the lenient direction and you overstate accuracy. Get it
wrong in the strict direction and you throw away correct queries. Both make the
project's headline delta meaningless, so the rules are made explicit here
rather than left to whatever the database happened to return.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence

# Two floats are considered equal within this relative tolerance. Aggregates
# over large money columns accumulate float error, so exact equality is too
# strict; anything looser starts calling genuinely different figures equal.
FLOAT_REL_TOL = 1e-6
FLOAT_ABS_TOL = 1e-9


class Match(str, Enum):
    """Outcome of comparing one predicted result set against gold."""

    EXACT = "exact"
    # Same rows, but the predicted query returned them in a different order
    # while gold explicitly asked for an order. Reported separately rather than
    # folded into MISMATCH, because it is a different kind of mistake.
    ORDER_DIFFERS = "order_differs"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class _BoolCell:
    """A boolean, wrapped so it cannot compare equal to a number.

    Python treats ``True == 1 == 1.0`` and hashes them identically, so a bare
    bool in a result row would silently satisfy a comparison against a count of
    one -- including as a dict key, which is how the rows are counted here.
    Wrapping in a distinct type is what actually keeps them apart; the
    isinstance check in ``_normalise_scalar`` alone does not.
    """

    value: bool


@dataclass(frozen=True)
class ComparisonResult:
    match: Match
    reason: str
    pred_rows: int
    gold_rows: int

    @property
    def is_correct(self) -> bool:
        return self.match is Match.EXACT

    def __str__(self) -> str:  # pragma: no cover - human output only
        return f"{self.match.value}: {self.reason} (pred={self.pred_rows}, gold={self.gold_rows})"


def _normalise_scalar(value: Any) -> Any:
    """Reduce one cell to a canonical form.

    The goal is that two cells which any reasonable person would call "the same
    answer" normalise to the same Python object.
    """
    if value is None:
        return None

    # NaN is not None, is not equal to itself, and turns up wherever a database
    # driver decided an absent number was a float. Treat it as NULL.
    if isinstance(value, float) and math.isnan(value):
        return None

    if isinstance(value, bool):
        # Must precede the int branch: bool is a subclass of int, and True
        # collapsing to 1 would make a boolean flag compare equal to a count.
        return _BoolCell(value)

    if isinstance(value, Decimal):
        return _normalise_number(float(value))

    if isinstance(value, (int, float)):
        return _normalise_number(float(value))

    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            return bytes(value)

    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()

    if isinstance(value, str):
        # Whitespace and case differences in a label are formatting, not a
        # different answer. Numeric strings are folded into numbers so that
        # '1500' and 1500 do not count as a mismatch.
        stripped = value.strip()
        as_number = _try_number(stripped)
        if as_number is not None:
            return as_number
        return stripped.casefold()

    return value


def _try_number(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.replace(",", "")
    try:
        return _normalise_number(float(cleaned))
    except ValueError:
        return None


def _normalise_number(number: float) -> float:
    # Collapse -0.0 to 0.0 so sign of zero never decides a comparison.
    return 0.0 if number == 0 else number


def _values_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=FLOAT_REL_TOL, abs_tol=FLOAT_ABS_TOL)
    return left == right


def _row_key(row: Sequence[Any], sort_cells: bool) -> tuple:
    """Canonical, hashable form of one row.

    ``sort_cells`` makes the comparison blind to column order, which is what we
    want when gold says ``SELECT name, total`` and the model wrote
    ``SELECT total, name``. Values are sorted by their string form because a row
    can mix types and Python will not order those against each other.
    """
    cells = [_normalise_scalar(cell) for cell in row]
    if sort_cells:
        cells.sort(key=lambda cell: (cell is None, str(cell)))
    return tuple(cells)


def _multiset(rows: Sequence[Sequence[Any]], sort_cells: bool) -> dict[tuple, int]:
    """Count rows. A multiset, not a set: duplicate rows are part of the answer.

    Collapsing to a set here would let a query that lost its GROUP BY compare
    equal to one that did not.
    """
    counts: dict[tuple, int] = {}
    for row in rows:
        key = _row_key(row, sort_cells)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _multisets_equal(left: dict[tuple, int], right: dict[tuple, int]) -> bool:
    if left.keys() != right.keys():
        # Keys may still be float-equal without being hash-equal, so fall back
        # to a pairwise walk before declaring a mismatch.
        return _multisets_equal_slow(left, right)
    return all(left[key] == right[key] for key in left)


def _multisets_equal_slow(left: dict[tuple, int], right: dict[tuple, int]) -> bool:
    if sum(left.values()) != sum(right.values()):
        return False
    remaining = dict(right)
    for key, count in left.items():
        matched = None
        for candidate in remaining:
            if len(candidate) == len(key) and all(
                _values_equal(a, b) for a, b in zip(key, candidate)
            ):
                matched = candidate
                break
        if matched is None or remaining[matched] != count:
            return False
        del remaining[matched]
    return not remaining


def compare_result_sets(
    pred_rows: Sequence[Sequence[Any]],
    gold_rows: Sequence[Sequence[Any]],
    *,
    order_matters: bool = False,
    column_order_matters: bool = False,
) -> ComparisonResult:
    """Compare a predicted result set against gold.

    ``order_matters`` should be True only when the gold query has an explicit
    ORDER BY, because that is the only case where the caller actually asked for
    a particular sequence. Set it from the gold SQL, never from the prediction.

    ``column_order_matters`` defaults to False: selecting the right values in a
    different order is a formatting difference, not a wrong answer.
    """
    sort_cells = not column_order_matters
    n_pred, n_gold = len(pred_rows), len(gold_rows)

    # An empty prediction matching empty gold is a real match, but it is also
    # the single most common way an eval flatters itself: a query that silently
    # filters everything away scores a point. Callers should treat a high rate
    # of empty-empty matches as a signal to inspect the gold set, so it is
    # named explicitly in the reason string rather than reported as an
    # ordinary pass.
    if n_pred == 0 and n_gold == 0:
        return ComparisonResult(Match.EXACT, "both empty", 0, 0)

    if n_pred != n_gold:
        return ComparisonResult(
            Match.MISMATCH, f"row count differs ({n_pred} vs {n_gold})", n_pred, n_gold
        )

    if n_gold and len({len(row) for row in gold_rows} | {len(row) for row in pred_rows}) > 1:
        return ComparisonResult(
            Match.MISMATCH, "column count differs", n_pred, n_gold
        )

    pred_ms = _multiset(pred_rows, sort_cells)
    gold_ms = _multiset(gold_rows, sort_cells)
    if not _multisets_equal(pred_ms, gold_ms):
        return ComparisonResult(Match.MISMATCH, "row values differ", n_pred, n_gold)

    if order_matters:
        pred_seq = [_row_key(row, sort_cells) for row in pred_rows]
        gold_seq = [_row_key(row, sort_cells) for row in gold_rows]
        same_order = len(pred_seq) == len(gold_seq) and all(
            len(a) == len(b) and all(_values_equal(x, y) for x, y in zip(a, b))
            for a, b in zip(pred_seq, gold_seq)
        )
        if not same_order:
            return ComparisonResult(
                Match.ORDER_DIFFERS, "same rows, different order", n_pred, n_gold
            )

    return ComparisonResult(Match.EXACT, "rows match", n_pred, n_gold)


def gold_requires_order(sql: str) -> bool:
    """Whether gold SQL asks for a specific row order.

    Deliberately crude, and deliberately conservative: a bare ORDER BY inside a
    subquery or a window function does not constrain the order of the final
    result, so only a trailing ORDER BY on the outermost query counts. When in
    doubt this returns False, which is the lenient direction for row order and
    the strict direction for row values.

    String literals are blanked first. Without that, `SELECT 'ORDER BY x'`
    was read as a real ORDER BY and the comparison then demanded a row order
    the query never asked for -- turning a correct answer into a reported
    failure.
    """
    lowered = " ".join(_blank_string_literals(sql).lower().split())
    idx = lowered.rfind("order by")
    if idx == -1:
        return False
    # An ORDER BY that still has an unclosed paren before it belongs to a
    # subquery or a window function, not the outer statement.
    prefix = lowered[:idx]
    return prefix.count("(") == prefix.count(")")


def _blank_string_literals(sql: str) -> str:
    """Replace the contents of single-quoted literals with spaces.

    Keeps every character position and both quote marks, so paren balancing
    downstream is unaffected. Handles the SQL '' escape for an embedded quote.
    """
    out: list[str] = []
    inside = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if not inside:
            out.append(ch)
            if ch == "'":
                inside = True
        else:
            if ch == "'":
                # '' inside a literal is an escaped quote, not the end of it.
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("  ")
                    i += 2
                    continue
                out.append(ch)
                inside = False
            else:
                out.append(" ")
        i += 1
    return "".join(out)
