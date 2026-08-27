"""The self-correction loop.

generate -> EXPLAIN -> execute -> repair on error, capped at three attempts.

EXPLAIN comes before execute because it is the cheap gate: it catches unknown
columns, bad joins and type errors for the price of a plan, before the query is
allowed to spend the execution timeout. On a schema this wide most first
attempts fail on a column name, and catching that without running anything is
most of the loop's value.

Every attempt is logged. The number worth reporting from this module is the
REPAIR-RESCUE RATE: of the queries that failed on the first attempt, what
fraction ended up correct. That figure is interesting in both directions -- a
high rate means the loop is earning its cost, a low one means the model is
failing for reasons an error message cannot fix, which is itself the finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import Database, ExecResult, ExecStatus
from .generate import Provider, ProviderError, generate_sql

MAX_ATTEMPTS = 3


@dataclass
class Attempt:
    n: int
    sql: str
    stage: str                 # 'extract' | 'explain' | 'execute'
    status: str
    error: str | None = None
    raw: str = ""

    @property
    def failed(self) -> bool:
        return self.status != "ok"


@dataclass
class Resolution:
    """The outcome of running one question through the loop."""

    sql: str | None
    result: ExecResult | None
    attempts: list[Attempt] = field(default_factory=list)
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None and self.result.ok

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def needed_repair(self) -> bool:
        """Whether the first attempt failed."""
        return bool(self.attempts) and self.attempts[0].failed

    @property
    def repair_rescued(self) -> bool:
        """First attempt failed, a later one succeeded."""
        return self.needed_repair and self.succeeded


def resolve(
    question: str,
    db: Database,
    schema: str,
    provider: Provider,
    *,
    semantic: str | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> Resolution:
    """Generate SQL for one question, repairing on error up to `max_attempts`."""
    attempts: list[Attempt] = []
    prior_sql: str | None = None
    prior_error: str | None = None

    for n in range(1, max_attempts + 1):
        try:
            sql, raw = generate_sql(
                question, schema, provider,
                semantic=semantic, prior_sql=prior_sql, prior_error=prior_error,
            )
        except ProviderError as err:
            # The provider being down is an infrastructure failure, not a
            # model mistake. Retrying would just log the same thing three
            # times and pollute the repair-rescue rate.
            attempts.append(Attempt(n, "", "provider", "error", str(err)))
            return Resolution(None, None, attempts, error=str(err))

        if not sql.strip():
            attempts.append(Attempt(n, "", "extract", "empty",
                                    "model returned no SQL", raw))
            prior_sql, prior_error = "", "You returned no SQL. Return one SELECT statement."
            continue

        # Cheap gate first.
        planned = db.explain(sql)
        if not planned.ok:
            attempts.append(Attempt(n, sql, "explain", planned.status.value,
                                    planned.error, raw))
            prior_sql, prior_error = sql, planned.error
            continue

        executed = db.execute(sql)
        attempts.append(Attempt(n, sql, "execute", executed.status.value,
                                executed.error, raw))
        if executed.ok:
            return Resolution(sql, executed, attempts)

        prior_sql, prior_error = sql, executed.error

    last = attempts[-1] if attempts else None
    return Resolution(
        last.sql if last else None, None, attempts,
        error=last.error if last else "no attempts made",
    )


def repair_stats(resolutions: list[Resolution]) -> dict[str, float | int]:
    """Aggregate repair behaviour across a run."""
    total = len(resolutions)
    needed = [r for r in resolutions if r.needed_repair]
    rescued = [r for r in needed if r.succeeded]
    first_pass = [r for r in resolutions if r.attempts and not r.attempts[0].failed]

    return {
        "questions": total,
        "first_attempt_ok": len(first_pass),
        "needed_repair": len(needed),
        "repair_rescued": len(rescued),
        "repair_rescue_rate": (len(rescued) / len(needed)) if needed else 0.0,
        "mean_attempts": (
            sum(r.attempt_count for r in resolutions) / total if total else 0.0
        ),
    }


def failure_reasons(resolutions: list[Resolution], limit: int = 10) -> list[tuple[str, int]]:
    """Most common first-attempt errors, normalised enough to group.

    Useful for the writeup: it says WHY the model failed, which is more
    interesting than how often.
    """
    counts: dict[str, int] = {}
    for res in resolutions:
        if not res.attempts or not res.attempts[0].failed:
            continue
        error = (res.attempts[0].error or "unknown").strip()
        # Strip the specific identifier so 'Referenced column "foo"' and
        # 'Referenced column "bar"' group together.
        key = error.split("\n")[0][:90]
        for quote in ('"', "'"):
            parts = key.split(quote)
            if len(parts) >= 3:
                key = quote.join(parts[:1] + ["X"] + parts[2:])
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
