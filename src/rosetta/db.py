"""Execution sandbox.

Everything the model writes is run through here, and nothing else in the
project is allowed to touch the database directly. Three guarantees:

1. The connection is read-only, so a generated statement cannot modify data.
2. Only SELECT-shaped statements are accepted, checked by parsing rather than
   by looking for keywords, so a comment or a CTE cannot smuggle a write past
   the check.
3. Every query has a wall-clock ceiling. A model that writes an accidental
   cross join on a multi-year table will otherwise hang the eval run.

The distinction this module exists to preserve is between a query that *failed*
and a query that *ran and returned the wrong thing*. The first is safe: the
user sees an error. The second is the one that ends up in a slide deck. Keeping
them apart here is what makes the confidently-wrong rate measurable at all.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

import duckdb
import sqlglot
from sqlglot import expressions as exp

DEFAULT_TIMEOUT_SECONDS = 30.0

# Statement types allowed to reach the database. Anything else is rejected
# before execution, whatever the connection's own permissions would have done.
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)


class ExecStatus(str, Enum):
    OK = "ok"
    ERROR = "error"          # the database refused it: syntax, unknown column, type
    TIMEOUT = "timeout"      # exceeded the wall-clock ceiling
    REJECTED = "rejected"    # never sent: not a read-only SELECT


@dataclass
class ExecResult:
    status: ExecStatus
    rows: list[tuple] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status is ExecStatus.OK

    @property
    def row_count(self) -> int:
        return len(self.rows)


class StatementRejected(Exception):
    """Raised when SQL is not a read-only SELECT."""


def assert_read_only(sql: str, dialect: str = "duckdb") -> None:
    """Parse the SQL and reject anything that is not a read-only SELECT.

    Parsing rather than keyword-matching matters: ``SELECT 1; DROP TABLE t``
    and ``/* SELECT */ DELETE FROM t`` both defeat a substring check, and both
    are the kind of thing a repair loop can produce by accident when it is
    handed an error message and told to try again.
    """
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as err:  # sqlglot raises several unrelated types
        raise StatementRejected(f"could not parse SQL: {err}") from err

    statements = [s for s in statements if s is not None]
    if not statements:
        raise StatementRejected("empty statement")
    if len(statements) > 1:
        raise StatementRejected(
            f"expected a single statement, got {len(statements)}"
        )

    root = statements[0]
    # A WITH ... SELECT parses as a Select carrying a `with` arg, so unwrapping
    # is not needed; but a bare CTE or a parenthesised union can arrive as
    # Subquery, which is why it is in the allow-list.
    if not isinstance(root, _ALLOWED_ROOTS):
        raise StatementRejected(
            f"only read-only SELECT statements are allowed, got {type(root).__name__.upper()}"
        )

    # Belt and braces: even inside a SELECT tree, refuse anything that mutates.
    for node in root.walk():
        if isinstance(
            node,
            (
                exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
                exp.Alter, exp.TruncateTable, exp.Merge,
            ),
        ):
            raise StatementRejected(
                f"statement contains a {type(node).__name__.upper()} node"
            )


class Database:
    """Read-only DuckDB handle with per-query timeouts."""

    def __init__(self, path: str | Path, *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.path = Path(path)
        self.timeout = timeout
        if not self.path.exists():
            raise FileNotFoundError(
                f"database not found at {self.path} - run scripts/load_data.py first"
            )
        # read_only=True is the real guard; assert_read_only is the fast, and
        # more legible, one. Both are kept because they fail differently: the
        # connection raises at execution time, the parser raises before we
        # spend a timeout budget on the query.
        self._con = duckdb.connect(str(self.path), read_only=True)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def execute(self, sql: str, *, timeout: float | None = None) -> ExecResult:
        """Run one read-only query under a wall-clock ceiling."""
        try:
            assert_read_only(sql)
        except StatementRejected as err:
            return ExecResult(ExecStatus.REJECTED, error=str(err))
        return self._run(sql, timeout)

    def _run(self, sql: str, timeout: float | None = None) -> ExecResult:
        """Execute without re-validating.

        Separate from execute() because EXPLAIN has to reach the database with
        its prefix attached, and sqlglot parses `EXPLAIN SELECT ...` as a
        Command rather than a Select -- so validating the prefixed string
        rejects every plan. The inner SQL is validated by the caller before it
        gets here; this is the only path that skips the check, and it is
        private for that reason.
        """
        import time

        limit = self.timeout if timeout is None else timeout
        timed_out = threading.Event()

        def _interrupt() -> None:
            timed_out.set()
            try:
                self._con.interrupt()
            except Exception:
                # Interrupting a connection that already finished is fine.
                pass

        watchdog = threading.Timer(limit, _interrupt)
        watchdog.daemon = True
        started = time.perf_counter()
        watchdog.start()
        try:
            cursor = self._con.execute(sql)
            rows = cursor.fetchall()
            columns = [d[0] for d in (cursor.description or [])]
            elapsed = time.perf_counter() - started
            return ExecResult(ExecStatus.OK, rows, columns, elapsed_seconds=elapsed)
        except Exception as err:
            elapsed = time.perf_counter() - started
            if timed_out.is_set():
                return ExecResult(
                    ExecStatus.TIMEOUT,
                    error=f"exceeded {limit:g}s",
                    elapsed_seconds=elapsed,
                )
            return ExecResult(
                ExecStatus.ERROR, error=str(err).strip(), elapsed_seconds=elapsed
            )
        finally:
            watchdog.cancel()

    def explain(self, sql: str, *, timeout: float | None = None) -> ExecResult:
        """Plan the query without running it.

        This is the cheap first gate in the self-correction loop: it catches
        unknown columns, bad joins and type errors for the cost of a parse,
        before the query is allowed to spend the execution budget.
        """
        try:
            assert_read_only(sql)
        except StatementRejected as err:
            return ExecResult(ExecStatus.REJECTED, error=str(err))
        return self._run(f"EXPLAIN {sql}", timeout)

    def list_tables(self) -> list[str]:
        result = self.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        )
        return [row[0] for row in result.rows]

    def describe_table(self, table: str) -> list[tuple[str, str]]:
        """Return (column_name, data_type) pairs for one table."""
        result = self.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema = 'main' AND table_name = '{table}' "
            "ORDER BY ordinal_position"
        )
        return [(row[0], row[1]) for row in result.rows]

    def column_count(self) -> int:
        result = self.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'main'"
        )
        return int(result.rows[0][0]) if result.rows else 0
