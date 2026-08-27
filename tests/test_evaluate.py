"""Tests for the outcome taxonomy and the pipeline, without a model.

A stub provider returns whatever SQL the test wants, which makes it possible to
verify the thing that actually matters here: that a query which runs cleanly
and returns the wrong number is classified CONFIDENTLY_WRONG rather than
counted as a pass.

That classification is the project's entire claim. It should be tested against
known inputs, not inferred from a model run.

Runs under pytest, or standalone with `python tests/test_evaluate.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rosetta.db import Database                        # noqa: E402
from rosetta.evaluate import Outcome, classify, run    # noqa: E402
from rosetta.pipeline import AnswerKind, Pipeline      # noqa: E402

DB_PATH = ROOT / "data" / "rosetta.duckdb"


class StubProvider:
    """Returns canned responses in order, cycling on the last one."""

    def __init__(self, *responses: str):
        self.responses = list(responses) or [""]
        self.calls = 0

    @property
    def name(self) -> str:
        return "stub"

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[index]


def _db():
    if not DB_PATH.exists():
        return None
    return Database(DB_PATH)


CORRECT_SQL = (
    "SELECT SUM(a.be_amount_crore) FROM budget_allocations a "
    "JOIN ministries m ON a.ministry_id = m.ministry_id "
    "WHERE a.fiscal_year = '2019-20' AND m.ministry_name = 'Ministry of Railways'"
)

# Identical except for the unit column: exactly 100x out, runs cleanly.
UNIT_TRAP_SQL = CORRECT_SQL.replace("be_amount_crore", "be_amount_lakh")

QUESTION = {
    "id": "TEST",
    "category": "answerable",
    "expect": "answer",
    "question": "What was the Budget Estimate for the Ministry of Railways in 2019-20?",
    "gold_sql": CORRECT_SQL,
}


def test_matching_result_is_correct():
    db = _db()
    if db is None:
        return
    pipe = Pipeline(db=db, provider=StubProvider(CORRECT_SQL), use_semantic_layer=False)
    answer = pipe.ask(QUESTION["question"])
    outcome, _ = classify(answer, QUESTION, db)
    db.close()
    assert outcome is Outcome.CORRECT


def test_clean_wrong_number_is_confidently_wrong():
    # The whole point. This query runs, returns one row, one plausible number,
    # and is 100x wrong. An execution-accuracy-only eval scores it as a pass
    # for having executed.
    db = _db()
    if db is None:
        return
    pipe = Pipeline(db=db, provider=StubProvider(UNIT_TRAP_SQL), use_semantic_layer=False)
    answer = pipe.ask(QUESTION["question"])
    outcome, _ = classify(answer, QUESTION, db)
    db.close()
    assert answer.kind is AnswerKind.ANSWERED, "the trap query must actually run"
    assert outcome is Outcome.CONFIDENTLY_WRONG


def test_broken_sql_is_errored_not_wrong():
    # A visible failure is safe. It must not be scored as confidently wrong.
    db = _db()
    if db is None:
        return
    pipe = Pipeline(
        db=db,
        provider=StubProvider("SELECT SUM(no_such_column) FROM budget_allocations"),
        use_semantic_layer=False,
    )
    answer = pipe.ask(QUESTION["question"])
    outcome, _ = classify(answer, QUESTION, db)
    db.close()
    assert outcome is Outcome.ERRORED


def test_refusing_an_ambiguous_question_is_right():
    db = _db()
    if db is None:
        return
    question = {
        "id": "TEST2", "category": "ambiguous", "expect": "refuse",
        "question": "How much did the government spend in 2019-20?",
    }
    pipe = Pipeline(db=db, provider=StubProvider(CORRECT_SQL), use_semantic_layer=True)
    answer = pipe.ask(question["question"])
    outcome, _ = classify(answer, question, db)
    db.close()
    assert outcome is Outcome.REFUSED_RIGHTLY


def test_answering_an_ambiguous_question_is_confidently_wrong():
    # The baseline arm has no semantic layer, so it answers everything --
    # including questions with no single certified answer. Producing a number
    # there counts as confidently wrong even though the number is a valid
    # reading, because the user cannot tell which reading they got.
    db = _db()
    if db is None:
        return
    question = {
        "id": "TEST3", "category": "ambiguous", "expect": "refuse",
        "question": "How much did the government spend in 2019-20?",
    }
    pipe = Pipeline(db=db, provider=StubProvider(CORRECT_SQL), use_semantic_layer=False)
    answer = pipe.ask(question["question"])
    outcome, _ = classify(answer, question, db)
    db.close()
    assert answer.kind is AnswerKind.ANSWERED
    assert outcome is Outcome.CONFIDENTLY_WRONG


def test_refusing_an_answerable_question_is_wrong_refusal():
    # Tracked so that a system which refuses everything cannot look good.
    db = _db()
    if db is None:
        return
    question = {
        "id": "TEST4", "category": "answerable", "expect": "answer",
        "question": "What is the budget for the Ministry of Railways in 2020-21?",
        "gold_sql": CORRECT_SQL,
    }
    pipe = Pipeline(db=db, provider=StubProvider(CORRECT_SQL), use_semantic_layer=True)
    answer = pipe.ask(question["question"])
    outcome, _ = classify(answer, question, db)
    db.close()
    assert outcome is Outcome.REFUSED_WRONGLY


def test_broken_gold_query_does_not_score_the_model():
    # If the gold is broken, the model must not be blamed for disagreeing.
    db = _db()
    if db is None:
        return
    question = dict(QUESTION, gold_sql="SELECT * FROM table_that_does_not_exist")
    pipe = Pipeline(db=db, provider=StubProvider(CORRECT_SQL), use_semantic_layer=False)
    answer = pipe.ask(question["question"])
    outcome, detail = classify(answer, question, db)
    db.close()
    assert outcome is Outcome.ERRORED
    assert "GOLD QUERY BROKEN" in detail


def test_repair_loop_recovers_and_is_recorded():
    db = _db()
    if db is None:
        return
    provider = StubProvider(
        "SELECT SUM(no_such_column) FROM budget_allocations",  # attempt 1 fails
        CORRECT_SQL,                                            # attempt 2 works
    )
    pipe = Pipeline(db=db, provider=provider, use_semantic_layer=False)
    answer = pipe.ask(QUESTION["question"])
    outcome, _ = classify(answer, QUESTION, db)
    db.close()
    assert outcome is Outcome.CORRECT
    assert answer.resolution is not None
    assert answer.resolution.needed_repair
    assert answer.resolution.repair_rescued
    assert answer.resolution.attempt_count == 2


def test_report_headline_rates():
    db = _db()
    if db is None:
        return
    questions = [
        dict(QUESTION, id="A"),
        {"id": "B", "category": "ambiguous", "expect": "refuse",
         "question": "How much did the government spend in 2019-20?"},
    ]
    pipe = Pipeline(db=db, provider=StubProvider(CORRECT_SQL), use_semantic_layer=True)
    report = run(pipe, questions, progress=False)
    db.close()
    assert report.total == 2
    assert report.count(Outcome.CORRECT) == 1
    assert report.count(Outcome.REFUSED_RIGHTLY) == 1
    assert report.confidently_wrong_rate == 0.0
    assert report.refusal_recall == 1.0


def test_the_unit_trap_really_is_100x():
    # Guards the premise of test_clean_wrong_number_is_confidently_wrong: if
    # the two columns ever stop being exactly 100x apart, that test would pass
    # for the wrong reason.
    db = _db()
    if db is None:
        return
    crore = db.execute(CORRECT_SQL).rows[0][0]
    lakh = db.execute(UNIT_TRAP_SQL).rows[0][0]
    db.close()
    assert crore and lakh
    assert abs(lakh / crore - 100) < 1e-6


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"database missing at {DB_PATH}; run scripts/load_data.py first")
        sys.exit(1)
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
