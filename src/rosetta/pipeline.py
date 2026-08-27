"""End to end: question in, answer or refusal out.

    question
       |
       v
    ambiguity.assess()  -- rule-based, deterministic, runs before any model call
       |
       +-- AMBIGUOUS ------> ask back, listing the certified readings
       +-- UNANSWERABLE ---> say why, do NOT return zero
       |
       v  ANSWERABLE
    correct.resolve()   -- generate -> EXPLAIN -> execute -> repair (max 3)
       |
       v
    answer + the SQL, always shown

The two ablation arms differ in exactly two switches, both held here:
`use_semantic_layer` controls whether the domain rules reach the prompt, and
whether ambiguity detection runs at all. The baseline arm answers everything,
because a system without a semantic layer has no certified readings to be
ambiguous between -- which is precisely why it returns confident wrong numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from . import ambiguity
from .correct import Resolution, resolve
from .db import Database, ExecResult
from .generate import Provider
from .semantic import SemanticLayer, load_semantic_layer, schema_description, semantic_context


class AnswerKind(str, Enum):
    ANSWERED = "answered"
    REFUSED = "refused"
    FAILED = "failed"          # generation could not produce runnable SQL


@dataclass
class Answer:
    kind: AnswerKind
    question: str
    sql: str | None = None
    result: ExecResult | None = None
    refusal: str = ""
    refusal_rule: str = ""
    resolution: Resolution | None = None
    arm: str = ""
    # Non-empty when the answer rested on a certified default under the
    # DISCLOSE policy. An answer carrying this is qualified, not confident,
    # which is the distinction the evaluation turns on.
    disclosure: str = ""

    @property
    def was_disclosed(self) -> bool:
        return bool(self.disclosure)

    @property
    def rows(self) -> list[tuple]:
        return self.result.rows if self.result else []

    def summary(self) -> str:
        if self.kind is AnswerKind.REFUSED:
            return f"REFUSED ({self.refusal_rule})"
        if self.kind is AnswerKind.FAILED:
            attempts = self.resolution.attempt_count if self.resolution else 0
            return f"FAILED after {attempts} attempt(s)"
        n = len(self.rows)
        return f"ANSWERED, {n} row(s)"


# How an assumed dimension is expressed to the generator. Must agree with
# ambiguity.CERTIFIED_DEFAULTS -- if the disclosure says "Budget Estimate" and
# the prompt does not pin be_amount_crore, the note and the number disagree,
# which is a worse failure than either policy alone.
# A pin must fix EVERY column the answer depends on, not just the one the
# dimension is named after. An earlier version of the receipts pin named only
# the filter; the model applied it correctly and then summed
# net_to_centre_crore instead of be_amount_crore, producing an answer whose
# stated assumption and actual figure disagreed. That is a worse failure than
# refusing, because the label makes it look checked.
_PIN_TEXT = {
    "estimate_basis": "Use be_amount_crore (Budget Estimate). Not RE, not actuals.",
    "entity_scope": "Include ALL spending entities. Do not filter entity_kind.",
    "receipts_borrowings": (
        "Filter is_revenue_receipt = TRUE (exclude borrowings) AND sum "
        "be_amount_crore -- not net_to_centre_crore, not gross_amount_crore."
    ),
}


def _pin(assumptions) -> str:
    lines = ["THE QUESTION IS UNDER-SPECIFIED. These readings are fixed for you:"]
    for a in assumptions:
        lines.append("  " + _PIN_TEXT.get(a.dimension, a.default_label))
    return "\n".join(lines)


@dataclass
class Pipeline:
    db: Database
    provider: Provider
    use_semantic_layer: bool = True
    # STRICT refuses an under-specified question; DISCLOSE answers it with a
    # certified default and states the assumption. See ambiguity.py.
    policy: str = ambiguity.STRICT
    layer: SemanticLayer = field(default_factory=load_semantic_layer)
    _schema: str | None = field(default=None, repr=False)
    _semantic: str | None = field(default=None, repr=False)

    @property
    def arm(self) -> str:
        if not self.use_semantic_layer:
            return "baseline"
        return "semantic" if self.policy == ambiguity.STRICT else "semantic-disclose"

    @property
    def schema(self) -> str:
        # Built once. It is identical across both arms by construction, which
        # is what keeps the ablation honest.
        if self._schema is None:
            self._schema = schema_description(self.db)
        return self._schema

    @property
    def semantic(self) -> str | None:
        if not self.use_semantic_layer:
            return None
        if self._semantic is None:
            # Pass the db so exact enum values are read from the data rather
            # than hardcoded -- a stale value list is worse than none, because
            # a wrong literal produces a query that runs and matches nothing.
            self._semantic = semantic_context(self.layer, db=self.db)
        return self._semantic

    def ask(self, question: str) -> Answer:
        disclosure = ""
        semantic = self.semantic

        if self.use_semantic_layer:
            verdict = ambiguity.assess(question, policy=self.policy)
            if verdict.should_refuse:
                return Answer(
                    AnswerKind.REFUSED,
                    question,
                    refusal=verdict.as_message(),
                    refusal_rule=verdict.rule,
                    arm=self.arm,
                )
            if verdict.assumptions:
                disclosure = verdict.disclosure()
                # The assumption has to reach the generator too, or the answer
                # is disclosed as one thing and computed as another.
                semantic = (semantic or "") + "\n\n" + _pin(verdict.assumptions)

        resolution = resolve(
            question, self.db, self.schema, self.provider, semantic=semantic
        )

        if resolution.succeeded:
            return Answer(
                AnswerKind.ANSWERED,
                question,
                sql=resolution.sql,
                result=resolution.result,
                resolution=resolution,
                arm=self.arm,
                disclosure=disclosure,
            )

        return Answer(
            AnswerKind.FAILED,
            question,
            sql=resolution.sql,
            resolution=resolution,
            arm=self.arm,
            disclosure=disclosure,
        )
