"""Tests for result-set comparison and the read-only guard.

Runs under pytest, or standalone with `python tests/test_compare.py`.
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rosetta.compare import (  # noqa: E402
    Match,
    compare_result_sets,
    gold_requires_order,
)
from rosetta.db import StatementRejected, assert_read_only  # noqa: E402


# --- the things that should count as the same answer ------------------------

def test_identical_rows_match():
    assert compare_result_sets([(1, "a")], [(1, "a")]).match is Match.EXACT


def test_int_and_float_match():
    # DuckDB may return a SUM as int or float depending on the column type.
    assert compare_result_sets([(1500,)], [(1500.0,)]).match is Match.EXACT


def test_decimal_matches_float():
    assert compare_result_sets([(Decimal("12.50"),)], [(12.5,)]).match is Match.EXACT


def test_numeric_string_matches_number():
    assert compare_result_sets([("1500",)], [(1500,)]).match is Match.EXACT


def test_whitespace_and_case_ignored_in_labels():
    assert compare_result_sets([("  Delhi ",)], [("delhi",)]).match is Match.EXACT


def test_column_order_ignored_by_default():
    # SELECT name, total vs SELECT total, name is a formatting difference.
    assert compare_result_sets([("a", 1)], [(1, "a")]).match is Match.EXACT


def test_column_order_respected_when_requested():
    result = compare_result_sets([("a", 1)], [(1, "a")], column_order_matters=True)
    assert result.match is Match.MISMATCH


def test_row_order_ignored_by_default():
    assert compare_result_sets([(2,), (1,)], [(1,), (2,)]).match is Match.EXACT


def test_float_tolerance():
    assert compare_result_sets([(0.1 + 0.2,)], [(0.3,)]).match is Match.EXACT


def test_date_matches_isoformat_string():
    assert compare_result_sets(
        [(dt.date(2023, 4, 1),)], [("2023-04-01",)]
    ).match is Match.EXACT


def test_null_and_nan_are_the_same():
    assert compare_result_sets([(None,)], [(float("nan"),)]).match is Match.EXACT


# --- the things that must NOT count as the same answer ----------------------

def test_different_values_mismatch():
    assert compare_result_sets([(1,)], [(2,)]).match is Match.MISMATCH


def test_row_count_mismatch():
    assert compare_result_sets([(1,)], [(1,), (2,)]).match is Match.MISMATCH


def test_duplicates_are_significant():
    # A query that lost its GROUP BY returns the same values more often. Set
    # semantics would call this correct; multiset semantics catches it.
    assert compare_result_sets([(1,), (1,)], [(1,)]).match is Match.MISMATCH


def test_bool_does_not_collapse_to_int():
    # True == 1 in Python. A boolean flag must not compare equal to a count.
    assert compare_result_sets([(True,)], [(1,)]).match is Match.MISMATCH


def test_null_is_not_zero():
    assert compare_result_sets([(None,)], [(0,)]).match is Match.MISMATCH


def test_order_differs_is_reported_separately():
    result = compare_result_sets([(2,), (1,)], [(1,), (2,)], order_matters=True)
    assert result.match is Match.ORDER_DIFFERS
    assert not result.is_correct


# --- the empty-set trap -----------------------------------------------------

def test_both_empty_matches_but_is_named():
    # This is a real match, and it is also how an eval flatters itself: a query
    # that filters everything away scores a point. The reason string has to say
    # so, because the audit pass greps for it.
    result = compare_result_sets([], [])
    assert result.match is Match.EXACT
    assert result.reason == "both empty"


def test_empty_prediction_against_nonempty_gold_mismatches():
    assert compare_result_sets([], [(1,)]).match is Match.MISMATCH


# --- ORDER BY detection -----------------------------------------------------

def test_trailing_order_by_detected():
    assert gold_requires_order("SELECT a FROM t ORDER BY a")


def test_no_order_by():
    assert not gold_requires_order("SELECT a FROM t")


def test_order_by_inside_subquery_ignored():
    # The inner ORDER BY does not constrain the order of the outer result.
    sql = "SELECT a FROM (SELECT a FROM t ORDER BY a)"
    assert not gold_requires_order(sql)


# --- read-only guard --------------------------------------------------------

def _rejects(sql: str) -> bool:
    try:
        assert_read_only(sql)
        return False
    except StatementRejected:
        return True


def test_plain_select_allowed():
    assert not _rejects("SELECT 1")


def test_cte_allowed():
    assert not _rejects("WITH x AS (SELECT 1 AS a) SELECT a FROM x")


def test_union_allowed():
    assert not _rejects("SELECT 1 UNION SELECT 2")


def test_delete_rejected():
    assert _rejects("DELETE FROM t")


def test_stacked_statement_rejected():
    # The classic: a valid SELECT followed by something else.
    assert _rejects("SELECT 1; DROP TABLE t")


def test_commented_write_rejected():
    assert _rejects("/* SELECT */ DELETE FROM t")


def test_create_rejected():
    assert _rejects("CREATE TABLE t AS SELECT 1")


def test_garbage_rejected():
    assert _rejects("not sql at all ((")


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as err:
            failed += 1
            print(f"FAIL  {name}  {err}")
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}  {type(err).__name__}: {err}")
        else:
            passed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
