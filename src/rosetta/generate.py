"""SQL generation.

The provider is pluggable on purpose. The project's headline claim is about
the semantic layer, not about which model wrote the SQL, and the cleanest way
to demonstrate that is to hold the layer fixed and swap the generator. Two are
wired here by default -- a general model and a code model -- so the comparison
is available without touching this file.

Nothing in this module decides whether a question *should* be answered. That
happens in ambiguity.py, before generation, and it is deliberately not the
model's call.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "qwen2.5-coder:7b"
FALLBACK_MODEL = "llama3.1:8b"


class Provider(Protocol):
    name: str

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str: ...


@dataclass
class OllamaProvider:
    """Local inference. No key, no rate limit, no per-call cost.

    Temperature defaults to 0: the eval re-runs the same questions many times
    and sampling noise would show up as movement in the headline number.
    """

    model: str = DEFAULT_MODEL
    url: str = OLLAMA_URL
    timeout: float = 180.0

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    def complete(self, prompt: str, *, temperature: float = 0.0) -> str:
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": 512,
                # A long schema plus the semantic layer needs real context.
                "num_ctx": 8192,
            },
        }).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return body.get("response", "")
        except urllib.error.URLError as err:
            raise ProviderError(f"{self.name} unreachable: {err}") from err
        except TimeoutError as err:
            raise ProviderError(f"{self.name} timed out after {self.timeout:g}s") from err


class ProviderError(RuntimeError):
    pass


def available_models(url: str = "http://127.0.0.1:11434/api/tags") -> list[str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        return [m["name"] for m in body.get("models", [])]
    except Exception:
        return []


def default_provider() -> OllamaProvider:
    """Prefer the code model; fall back to whatever is actually installed."""
    installed = available_models()
    for candidate in (DEFAULT_MODEL, FALLBACK_MODEL):
        if candidate in installed:
            return OllamaProvider(model=candidate)
    if installed:
        return OllamaProvider(model=installed[0])
    return OllamaProvider(model=DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------
BASE_INSTRUCTIONS = """You write DuckDB SQL for a question about the Indian Union Budget.

Rules:
- Return ONE SELECT statement and nothing else.
- No prose, no explanation, no markdown fences.
- Never write INSERT, UPDATE, DELETE, CREATE or DROP.
- Use only tables and columns that appear in the schema below.
"""


def build_prompt(
    question: str,
    schema: str,
    *,
    semantic: str | None = None,
    prior_sql: str | None = None,
    prior_error: str | None = None,
) -> str:
    """Assemble the prompt.

    The two ablation arms differ ONLY by whether `semantic` is passed. Schema,
    instructions, question and repair context are identical, so any difference
    in the measured result is attributable to the semantic layer rather than
    to prompt engineering.
    """
    parts = [BASE_INSTRUCTIONS, "SCHEMA:", schema]

    if semantic:
        parts += ["", "DOMAIN RULES (these override your assumptions):", semantic]

    if prior_sql and prior_error:
        parts += [
            "",
            "Your previous attempt failed.",
            f"SQL: {prior_sql}",
            f"Error: {prior_error}",
            "Fix the specific problem in the error. Do not rewrite from scratch.",
        ]

    parts += ["", f"QUESTION: {question}", "", "SQL:"]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_LEADING_LABEL = re.compile(r"^\s*(sql|query|answer)\s*:\s*", re.IGNORECASE)


def extract_sql(raw: str) -> str:
    """Pull a single SQL statement out of whatever the model returned.

    Models wrap SQL in fences, prefix it with 'SQL:', add a trailing
    explanation, or all three -- regardless of what the instructions said. This
    is cleanup, not parsing; the real validation is assert_read_only in db.py.
    """
    if not raw:
        return ""

    text = raw.strip()

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    text = _LEADING_LABEL.sub("", text).strip()

    # Cut anything before the first statement keyword.
    match = re.search(r"\b(WITH|SELECT)\b", text, re.IGNORECASE)
    if match:
        text = text[match.start():]

    # Cut at the first semicolon: one statement only.
    if ";" in text:
        text = text.split(";", 1)[0]

    # Drop trailing prose the model appended after the query.
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("--", "#")) and not lines:
            continue
        if lines and stripped and not re.match(
            r"^[\w\(\)\*,.'\"`\s=<>+\-/%|:\[\]]+$", stripped
        ):
            break
        lines.append(line)

    return "\n".join(lines).strip()


def generate_sql(
    question: str,
    schema: str,
    provider: Provider,
    *,
    semantic: str | None = None,
    prior_sql: str | None = None,
    prior_error: str | None = None,
) -> tuple[str, str]:
    """Return (sql, raw_response)."""
    prompt = build_prompt(
        question, schema, semantic=semantic,
        prior_sql=prior_sql, prior_error=prior_error,
    )
    raw = provider.complete(prompt)
    return extract_sql(raw), raw
