"""The evaluation harness.

This module defines the outcome taxonomy the whole project reports against, and
the taxonomy is the point. Most text-to-SQL evaluations collapse everything
into one number -- execution accuracy -- which treats these two failures as
equivalent:

    the query errored          the user sees an error and asks again
    the query returned 8,412   the user puts 8,412 in a report

They are not equivalent. The second is the one that does damage, and it is the
one nobody measures. So:

    CORRECT            answered, result matches gold
    CONFIDENTLY_WRONG  returned a clean, plausible, WRONG number     <- headline
    ERRORED            failed after repair attempts; visible, safe
    REFUSED_RIGHTLY    declined a question that has no single answer
    REFUSED_WRONGLY    declined a question it should have answered

CONFIDENTLY_WRONG has two sources, and both count:
  1. answered an answerable question and got the wrong rows
  2. answered a question that should have been refused -- producing a number
     for "how much did the government spend in 2019-20" is confidently wrong
     even if the number matches one of the readings, because the user has no
     way to know which reading they got

REFUSED_WRONGLY is tracked because a system that refuses everything scores
zero confidently-wrong while being useless. The pair has to be read together.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .compare import Match, compare_result_sets, gold_requires_order
from .correct import repair_stats
from .db import Database
from .pipeline import Answer, AnswerKind, Pipeline


class Outcome(str, Enum):
    CORRECT = "correct"
    CONFIDENTLY_WRONG = "confidently_wrong"
    # Right values, wrong sequence. Kept OUT of confidently_wrong on purpose:
    # a reader who gets the correct rows in a different order has not been
    # misled about any figure. Folding it into the headline would inflate the
    # number in this project's favour, which is the one thing the evaluation
    # cannot afford to do. It is excluded from CORRECT too, because the gold
    # query did ask for an order.
    WRONG_ORDER = "wrong_order"
    # Answered an under-specified question using a certified default, and
    # STATED the assumption. Deliberately not CONFIDENTLY_WRONG: the failure
    # being measured is a confident wrong number, and a figure delivered with
    # its assumption named is not confident. Deliberately not CORRECT either --
    # the reader still has to read the note, and a system that disclosed
    # everything would be as useless as one that refused everything. Counted
    # and reported on its own line so neither pretence is available.
    ANSWERED_DISCLOSED = "answered_disclosed"
    ERRORED = "errored"
    REFUSED_RIGHTLY = "refused_rightly"
    REFUSED_WRONGLY = "refused_wrongly"

    @property
    def is_dangerous(self) -> bool:
        return self is Outcome.CONFIDENTLY_WRONG

    @property
    def is_safe_failure(self) -> bool:
        return self in (Outcome.ERRORED, Outcome.REFUSED_WRONGLY, Outcome.WRONG_ORDER)


@dataclass
class QuestionResult:
    id: str
    category: str
    expect: str
    question: str
    outcome: Outcome
    arm: str
    sql: str | None = None
    detail: str = ""
    attempts: int = 0
    needed_repair: bool = False
    repair_rescued: bool = False
    elapsed_seconds: float = 0.0
    trap: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outcome"] = self.outcome.value
        return data


@dataclass
class RunReport:
    arm: str
    model: str
    results: list[QuestionResult] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    elapsed_seconds: float = 0.0

    # --- counts ------------------------------------------------------------
    def count(self, outcome: Outcome) -> int:
        return sum(1 for r in self.results if r.outcome is outcome)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def answerable(self) -> list[QuestionResult]:
        return [r for r in self.results if r.expect == "answer"]

    @property
    def refusable(self) -> list[QuestionResult]:
        return [r for r in self.results if r.expect == "refuse"]

    # --- headline numbers --------------------------------------------------
    @property
    def confidently_wrong_rate(self) -> float:
        """THE number. Share of all questions answered with a clean wrong figure."""
        return self.count(Outcome.CONFIDENTLY_WRONG) / self.total if self.total else 0.0

    @property
    def execution_accuracy(self) -> float:
        """Correct answers as a share of questions that have a right answer."""
        pool = self.answerable
        if not pool:
            return 0.0
        return sum(1 for r in pool if r.outcome is Outcome.CORRECT) / len(pool)

    @property
    def refusal_rate(self) -> float:
        refused = self.count(Outcome.REFUSED_RIGHTLY) + self.count(Outcome.REFUSED_WRONGLY)
        return refused / self.total if self.total else 0.0

    @property
    def refusal_precision(self) -> float:
        """Of the refusals made, how many were warranted."""
        right = self.count(Outcome.REFUSED_RIGHTLY)
        total = right + self.count(Outcome.REFUSED_WRONGLY)
        return right / total if total else 0.0

    @property
    def refusal_recall(self) -> float:
        """Of the questions needing a refusal, how many were handled safely.

        A disclosed answer counts as handled: the reader was told what was
        assumed, so they were not misled. Counting it as a miss would make
        DISCLOSE look like the baseline, which is exactly the distinction that
        matters -- the baseline answered the same questions with no note at all.
        """
        pool = self.refusable
        if not pool:
            return 0.0
        safe = (Outcome.REFUSED_RIGHTLY, Outcome.ANSWERED_DISCLOSED)
        return sum(1 for r in pool if r.outcome in safe) / len(pool)

    @property
    def repair_rescue_rate(self) -> float:
        """Of the queries whose first attempt failed, how many ended up correct.

        Interesting in both directions. High means the EXPLAIN-and-repair loop
        is earning its cost. Low means the model is failing for reasons an
        error message cannot fix -- which is a finding about the schema, not
        about the loop.
        """
        needed = [r for r in self.results if r.needed_repair]
        if not needed:
            return 0.0
        return sum(1 for r in needed if r.repair_rescued) / len(needed)

    @property
    def first_attempt_rate(self) -> float:
        """Share of generated queries that ran on the first try."""
        generated = [r for r in self.results if r.attempts > 0]
        if not generated:
            return 0.0
        return sum(1 for r in generated if not r.needed_repair) / len(generated)

    @property
    def mean_attempts(self) -> float:
        generated = [r for r in self.results if r.attempts > 0]
        if not generated:
            return 0.0
        return sum(r.attempts for r in generated) / len(generated)

    def by_trap(self) -> dict[str, tuple[int, int]]:
        """(correct, total) per trap type."""
        out: dict[str, tuple[int, int]] = {}
        for r in self.results:
            if not r.trap:
                continue
            correct, total = out.get(r.trap, (0, 0))
            out[r.trap] = (correct + (1 if r.outcome is Outcome.CORRECT else 0), total + 1)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "model": self.model,
            "total": self.total,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "headline": {
                "confidently_wrong_rate": round(self.confidently_wrong_rate, 4),
                "execution_accuracy": round(self.execution_accuracy, 4),
                "refusal_rate": round(self.refusal_rate, 4),
                "refusal_precision": round(self.refusal_precision, 4),
                "refusal_recall": round(self.refusal_recall, 4),
            },
            "repair": {
                "first_attempt_rate": round(self.first_attempt_rate, 4),
                "repair_rescue_rate": round(self.repair_rescue_rate, 4),
                "mean_attempts": round(self.mean_attempts, 2),
            },
            "counts": {o.value: self.count(o) for o in Outcome},
            "by_trap": {k: {"correct": c, "total": t} for k, (c, t) in self.by_trap().items()},
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
def load_questions(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("questions", [])


def classify(answer: Answer, question: dict[str, Any], db: Database) -> tuple[Outcome, str]:
    """Map one pipeline answer onto the taxonomy."""
    expect = question["expect"]

    if answer.kind is AnswerKind.REFUSED:
        if expect == "refuse":
            return Outcome.REFUSED_RIGHTLY, answer.refusal_rule
        return Outcome.REFUSED_WRONGLY, f"refused an answerable question ({answer.refusal_rule})"

    if answer.kind is AnswerKind.FAILED:
        last = answer.resolution.attempts[-1] if answer.resolution and answer.resolution.attempts else None
        return Outcome.ERRORED, (last.error or "no runnable SQL")[:120] if last else "no attempts"

    # It answered. Whether that is correct depends on what was asked.
    if expect == "refuse":
        if answer.was_disclosed:
            # Under DISCLOSE this question was answered on purpose, with the
            # assumption stated. Whether the FIGURE is right is still checked
            # below against the default reading's gold; getting here only means
            # answering was policy, not a lapse.
            gold_default = question.get("default_gold_sql")
            if gold_default:
                gold = db.execute(gold_default)
                if gold.ok:
                    cmp = compare_result_sets(
                        answer.rows, gold.rows,
                        order_matters=gold_requires_order(gold_default),
                    )
                    if cmp.is_correct:
                        return Outcome.ANSWERED_DISCLOSED, "matched the certified default"
                    return (
                        Outcome.CONFIDENTLY_WRONG,
                        f"disclosed an assumption then computed something else: {cmp.reason}",
                    )
            return Outcome.ANSWERED_DISCLOSED, "disclosed, no default gold to check against"
        return (
            Outcome.CONFIDENTLY_WRONG,
            "produced a number for a question with no single certified answer",
        )

    gold_sql = question.get("gold_sql")
    if not gold_sql:
        return Outcome.CONFIDENTLY_WRONG, "no gold SQL to check against"

    gold = db.execute(gold_sql)
    if not gold.ok:
        # The gold query itself is broken. Do not score the model on it.
        return Outcome.ERRORED, f"GOLD QUERY BROKEN: {gold.error}"

    comparison = compare_result_sets(
        answer.rows, gold.rows, order_matters=gold_requires_order(gold_sql)
    )
    if comparison.is_correct:
        return Outcome.CORRECT, comparison.reason
    if comparison.match is Match.ORDER_DIFFERS:
        return Outcome.WRONG_ORDER, comparison.reason
    return Outcome.CONFIDENTLY_WRONG, comparison.reason


def run(
    pipeline: Pipeline,
    questions: list[dict[str, Any]],
    *,
    progress: bool = True,
) -> RunReport:
    report = RunReport(arm=pipeline.arm, model=pipeline.provider.name)
    started = time.perf_counter()

    for i, question in enumerate(questions, start=1):
        q_started = time.perf_counter()
        answer = pipeline.ask(question["question"])
        outcome, detail = classify(answer, question, pipeline.db)
        elapsed = time.perf_counter() - q_started

        resolution = answer.resolution
        report.results.append(QuestionResult(
            id=question["id"],
            category=question["category"],
            expect=question["expect"],
            question=question["question"],
            outcome=outcome,
            arm=pipeline.arm,
            sql=answer.sql,
            detail=detail,
            attempts=resolution.attempt_count if resolution else 0,
            needed_repair=resolution.needed_repair if resolution else False,
            repair_rescued=resolution.repair_rescued if resolution else False,
            elapsed_seconds=round(elapsed, 2),
            trap=question.get("trap"),
        ))

        if progress:
            mark = {
                Outcome.CORRECT: "ok  ",
                Outcome.CONFIDENTLY_WRONG: "WRONG",
                Outcome.ERRORED: "err ",
                Outcome.REFUSED_RIGHTLY: "ref ",
                Outcome.REFUSED_WRONGLY: "REF?",
                Outcome.WRONG_ORDER: "ord ",
                Outcome.ANSWERED_DISCLOSED: "disc",
            }[outcome]
            print(f"  [{i:>2}/{len(questions)}] {question['id']:5s} {mark}  "
                  f"{elapsed:>5.1f}s  {detail[:56]}", flush=True)

    report.elapsed_seconds = time.perf_counter() - started
    return report


def write_report(report: RunReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")


def format_summary(report: RunReport) -> str:
    lines = [
        f"arm: {report.arm}   model: {report.model}   n={report.total}"
        f"   {report.elapsed_seconds:.0f}s",
        "",
        f"  CONFIDENTLY WRONG   {report.count(Outcome.CONFIDENTLY_WRONG):>3}"
        f"   {report.confidently_wrong_rate:>6.1%}   <- the number",
        f"  correct             {report.count(Outcome.CORRECT):>3}"
        f"   {report.execution_accuracy:>6.1%}   (of answerable)",
        f"  answered+disclosed  {report.count(Outcome.ANSWERED_DISCLOSED):>3}"
        f"        (assumption stated)",
        f"  wrong order         {report.count(Outcome.WRONG_ORDER):>3}"
        f"        (right values, wrong sequence)",
        f"  errored             {report.count(Outcome.ERRORED):>3}",
        f"  refused rightly     {report.count(Outcome.REFUSED_RIGHTLY):>3}",
        f"  refused wrongly     {report.count(Outcome.REFUSED_WRONGLY):>3}",
        "",
        f"  refusal precision   {report.refusal_precision:>6.1%}",
        f"  refusal recall      {report.refusal_recall:>6.1%}",
        "",
        f"  first attempt ran   {report.first_attempt_rate:>6.1%}",
        f"  repair rescued      {report.repair_rescue_rate:>6.1%}"
        f"   (of those that failed first)",
        f"  mean attempts       {report.mean_attempts:>6.2f}",
    ]
    traps = report.by_trap()
    if traps:
        lines += ["", "  by trap:"]
        for trap, (correct, total) in sorted(traps.items()):
            lines.append(f"    {trap:16s} {correct}/{total}")
    return "\n".join(lines)


def compare_arms(baseline: RunReport, semantic: RunReport) -> str:
    """The ablation. This table is the deliverable."""
    delta = semantic.confidently_wrong_rate - baseline.confidently_wrong_rate
    lines = [
        "",
        "=" * 66,
        "  THE ABLATION -- identical model, identical schema, layer on/off",
        "=" * 66,
        "",
        f"  {'':28s} {'baseline':>12s} {'semantic':>12s} {'delta':>10s}",
        f"  {'-' * 62}",
        f"  {'confidently wrong':28s} {baseline.confidently_wrong_rate:>11.1%} "
        f"{semantic.confidently_wrong_rate:>12.1%} {delta:>+10.1%}",
        f"  {'execution accuracy':28s} {baseline.execution_accuracy:>11.1%} "
        f"{semantic.execution_accuracy:>12.1%} "
        f"{semantic.execution_accuracy - baseline.execution_accuracy:>+10.1%}",
        f"  {'refusal recall':28s} {baseline.refusal_recall:>11.1%} "
        f"{semantic.refusal_recall:>12.1%} "
        f"{semantic.refusal_recall - baseline.refusal_recall:>+10.1%}",
        "",
        f"  n = {baseline.total} questions, of which "
        f"{len(baseline.refusable)} have no single certified answer.",
    ]
    return "\n".join(lines)
