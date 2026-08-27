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


@dataclass
class Pipeline:
    db: Database
    provider: Provider
    use_semantic_layer: bool = True
    layer: SemanticLayer = field(default_factory=load_semantic_layer)
    _schema: str | None = field(default=None, repr=False)
    _semantic: str | None = field(default=None, repr=False)

    @property
    def arm(self) -> str:
        return "semantic" if self.use_semantic_layer else "baseline"

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
        if self.use_semantic_layer:
            verdict = ambiguity.assess(question)
            if verdict.should_refuse:
                return Answer(
                    AnswerKind.REFUSED,
                    question,
                    refusal=verdict.as_message(),
                    refusal_rule=verdict.rule,
                    arm=self.arm,
                )

        resolution = resolve(
            question, self.db, self.schema, self.provider, semantic=self.semantic
        )

        if resolution.succeeded:
            return Answer(
                AnswerKind.ANSWERED,
                question,
                sql=resolution.sql,
                result=resolution.result,
                resolution=resolution,
                arm=self.arm,
            )

        return Answer(
            AnswerKind.FAILED,
            question,
            sql=resolution.sql,
            resolution=resolution,
            arm=self.arm,
        )
