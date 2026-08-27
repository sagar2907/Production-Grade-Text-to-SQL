"""Tests for ambiguity detection and refusal.

Two kinds of check here. Most are ordinary rule tests. The last group is
different and more important: it verifies that the availability constants in
ambiguity.py still match what the database actually contains. Those constants
are duplicated for readability, and duplicated facts drift -- if someone
extends the data by a year and forgets, the refusal rules start refusing
questions that now have answers, silently.

Runs under pytest, or standalone with `python tests/test_ambiguity.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta.ambiguity import (  # noqa: E402
    CERTIFIED_DEFAULTS, DISCLOSE, NO_ACTUALS, NO_RE, PLAN_ERA, STRICT,
    Outcome, assess, extract_years,
)
from rosetta.db import Database  # noqa: E402

DB_PATH = ROOT / "data" / "rosetta.duckdb"


# --- year extraction --------------------------------------------------------

def test_extracts_hyphenated_year():
    assert extract_years("spending in 2019-20") == ["2019-20"]


def test_extracts_full_hyphenated_year():
    assert extract_years("spending in 2019-2020") == ["2019-20"]


def test_extracts_bare_year():
    assert extract_years("spending in 2019") == ["2019-20"]


def test_extracts_fy_form():
    assert extract_years("what about FY20") == ["2019-20"]


def test_extracts_multiple_years():
    found = extract_years("compare 2016-17 and 2019-20")
    assert found == ["2016-17", "2019-20"]


def test_ignores_out_of_range_year():
    assert extract_years("spending in 1998") == []


# --- unanswerable: the Plan era --------------------------------------------

def test_plan_after_abolition_is_unanswerable():
    v = assess("What was Plan expenditure in 2019-20?")
    assert v.outcome is Outcome.UNANSWERABLE
    assert v.rule == "plan_era_boundary"


def test_non_plan_after_abolition_is_also_unanswerable():
    # Plan and Non-Plan were abolished together. An earlier version of the
    # rule excluded "non-plan" and let this through as answerable.
    v = assess("What was non-plan spending in 2020-21?")
    assert v.outcome is Outcome.UNANSWERABLE


def test_plan_inside_era_is_answerable():
    v = assess("What was Plan expenditure in 2016-17?")
    assert v.outcome is Outcome.ANSWERABLE


def test_plan_question_does_not_demand_estimate_basis():
    # plan_amount_crore has no BE/RE/Actual variant, so asking which basis
    # would be a refusal with no possible answer.
    v = assess("What was Plan expenditure in 2015-16?")
    assert v.outcome is Outcome.ANSWERABLE


# --- unanswerable: data not published yet -----------------------------------

def test_actuals_for_recent_year_unanswerable():
    v = assess("What was actually spent in 2023-24?")
    assert v.outcome is Outcome.UNANSWERABLE
    assert v.rule == "actuals_not_published"


def test_actuals_for_older_year_fine():
    v = assess("What was actually spent in 2019-20?")
    assert v.outcome is Outcome.ANSWERABLE


def test_re_for_latest_year_unanswerable():
    v = assess("Give me the Revised Estimate for 2023-24.")
    assert v.outcome is Outcome.UNANSWERABLE
    assert v.rule == "re_not_published"


# --- ambiguous --------------------------------------------------------------

def test_unpinned_basis_is_ambiguous():
    v = assess("What is the budget for the Ministry of Railways in 2020-21?")
    assert v.outcome is Outcome.AMBIGUOUS
    assert any(c.dimension == "estimate_basis" for c in v.clarifications)


def test_pinned_basis_is_answerable():
    v = assess("What was the Budget Estimate for the Ministry of Railways in 2020-21?")
    assert v.outcome is Outcome.ANSWERABLE


def test_unpinned_scope_on_aggregate_is_ambiguous():
    v = assess("How much did the government spend in 2019-20?")
    assert v.outcome is Outcome.AMBIGUOUS


def test_ambiguous_scheme_name():
    v = assess("How much was allocated to Swachh Bharat in 2019-20?")
    assert v.outcome is Outcome.AMBIGUOUS
    assert any(c.dimension == "scheme_identity" for c in v.clarifications)


def test_disambiguated_scheme_name_is_fine():
    v = assess(
        "What was the Budget Estimate for Swachh Bharat Mission - Gramin in 2019-20?"
    )
    assert v.outcome is Outcome.ANSWERABLE


def test_renamed_entity_over_a_span_is_ambiguous():
    v = assess("What did we spend on education each year since 2014?")
    assert v.outcome is Outcome.AMBIGUOUS


# --- scope rule exemptions --------------------------------------------------

def test_receipts_question_does_not_trigger_entity_scope():
    # Receipts are not booked to ministries, so entity_scope does not apply.
    v = assess("What is the total Budget Estimate for tax receipts in 2020-21?")
    assert v.outcome is Outcome.ANSWERABLE


def test_per_entity_breakdown_does_not_trigger_entity_scope():
    # "each ministry" already answers the scope question.
    v = assess(
        "For 2019-20, give each ministry's Budget Estimate alongside its "
        "total actual expenditure from the monthly table."
    )
    assert v.outcome is Outcome.ANSWERABLE


def test_explicit_scope_is_accepted():
    v = assess(
        "What did administrative ministries and departments spend in 2022-23 "
        "on a Budget Estimate basis, excluding functional lines?"
    )
    assert v.outcome is Outcome.ANSWERABLE


# --- refusal messages are usable -------------------------------------------

def test_ambiguous_message_lists_options():
    v = assess("How much did the government spend in 2019-20?")
    message = v.as_message()
    assert "more than one" in message.lower()
    assert "Budget Estimate" in message


def test_unanswerable_message_explains_why():
    v = assess("What was Plan expenditure in 2019-20?")
    message = v.as_message()
    assert "2017-18" in message


# --- policy: STRICT vs DISCLOSE --------------------------------------------

def test_disclose_answers_a_basis_only_question():
    q = "What is the budget for the Ministry of Railways in 2020-21?"
    assert assess(q, policy=STRICT).outcome is Outcome.AMBIGUOUS
    v = assess(q, policy=DISCLOSE)
    assert v.outcome is Outcome.ANSWERABLE
    assert v.assumptions and v.assumptions[0].dimension == "estimate_basis"


def test_disclose_states_what_it_assumed():
    # The disclosure is the whole difference between this and the baseline
    # silently guessing. If it is empty the policy is indefensible.
    v = assess("What is the budget for the Ministry of Railways in 2020-21?",
               policy=DISCLOSE)
    note = v.disclosure()
    assert "Budget Estimate" in note
    assert "Assumed" in note


def test_disclose_still_refuses_two_different_schemes():
    # Gramin and Urban are different schemes under different ministries.
    # There is no default to fall back on, so policy must not help here.
    q = "How much was allocated to Swachh Bharat in 2019-20?"
    for policy in (STRICT, DISCLOSE):
        assert assess(q, policy=policy).outcome is Outcome.AMBIGUOUS


def test_disclose_still_refuses_a_renamed_entity():
    q = "How much was spent on water ministry programmes over time?"
    assert assess(q, policy=DISCLOSE).outcome is Outcome.AMBIGUOUS


def test_policy_cannot_conjure_missing_data():
    # UNANSWERABLE is about what the data contains. No policy setting can
    # change that, and a policy that could would be the worst bug in the repo.
    for q in ("What was Plan expenditure in 2019-20?",
              "What was actually spent in 2023-24?",
              "Give me the Revised Estimate for 2023-24."):
        for policy in (STRICT, DISCLOSE):
            assert assess(q, policy=policy).outcome is Outcome.UNANSWERABLE


def test_strict_is_the_default_policy():
    q = "What is the budget for the Ministry of Railways in 2020-21?"
    assert assess(q).outcome is assess(q, policy=STRICT).outcome


def test_identity_dimensions_have_no_default():
    # If either of these ever gains a default, DISCLOSE starts silently
    # picking one of two different schemes, which is the failure this whole
    # project measures.
    assert "scheme_identity" not in CERTIFIED_DEFAULTS
    assert "entity_identity" not in CERTIFIED_DEFAULTS


def test_education_question_carries_entity_ambiguity():
    # Regression: the renamed-entity guard used to require the words
    # "ministry"/"department"/"spending on" and missed "spend on education".
    # STRICT hid it by refusing on basis anyway; DISCLOSE exposed it.
    v = assess("What did we spend on education each year since 2014?", policy=STRICT)
    assert "entity_identity" in {c.dimension for c in v.clarifications}


# --- the constants must match the database ---------------------------------

def _db_or_skip():
    if not DB_PATH.exists():
        print("  (database absent, skipping consistency checks)")
        return None
    return Database(DB_PATH)


def test_no_actuals_matches_database():
    db = _db_or_skip()
    if db is None:
        return
    rows = db.execute(
        "SELECT fiscal_year FROM fiscal_years WHERE actuals_available = FALSE"
    ).rows
    db.close()
    assert {r[0] for r in rows} == NO_ACTUALS, (
        f"ambiguity.NO_ACTUALS is {NO_ACTUALS} but the database says "
        f"{ {r[0] for r in rows} }"
    )


def test_no_re_matches_database():
    db = _db_or_skip()
    if db is None:
        return
    rows = db.execute(
        "SELECT fiscal_year FROM fiscal_years WHERE re_available = FALSE"
    ).rows
    db.close()
    assert {r[0] for r in rows} == NO_RE


def test_plan_era_matches_database():
    db = _db_or_skip()
    if db is None:
        return
    rows = db.execute(
        "SELECT fiscal_year FROM fiscal_years WHERE is_plan_regime = TRUE"
    ).rows
    db.close()
    assert {r[0] for r in rows} == PLAN_ERA


def test_plan_columns_actually_null_after_the_era():
    db = _db_or_skip()
    if db is None:
        return
    row = db.execute(
        "SELECT count(plan_amount_crore) FROM budget_allocations "
        "WHERE fiscal_year NOT IN ('2014-15','2015-16','2016-17')"
    ).rows[0]
    db.close()
    assert row[0] == 0, f"{row[0]} rows have plan_amount outside the Plan era"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as err:
            failed += 1
            print(f"FAIL  {name}\n      {err}")
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}  {type(err).__name__}: {err}")
        else:
            passed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
