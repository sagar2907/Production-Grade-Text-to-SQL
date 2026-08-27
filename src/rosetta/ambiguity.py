"""Ambiguity detection and refusal.

The claim this project is built to defend is that a question with more than one
certified reading should be asked back, not guessed at. This module decides
which questions those are.

It is deliberately RULE-BASED rather than model-based. Three reasons:

  1. It has to be deterministic. If the refusal decision itself came from a
     model, the confidently-wrong rate would move between runs for reasons
     unrelated to the thing being measured.
  2. It has to be auditable. Every refusal here can be traced to a named rule
     and a line in the semantic layer, which is what makes the refusal
     defensible to the person who got refused.
  3. It has to be cheap. It runs before generation, so it costs nothing when
     it fires.

Three outcomes:

  ANSWERABLE     exactly one certified reading -> generate SQL
  AMBIGUOUS      more than one certified reading -> ask back
  UNANSWERABLE   no reading has data -> say so, do not return zero

The third is not a nicety. Returning 0 for "what was actually spent in
2023-24" is a confidently wrong answer, because the actuals do not exist yet
and zero is a number a reader will believe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):
    ANSWERABLE = "answerable"
    AMBIGUOUS = "ambiguous"
    UNANSWERABLE = "unanswerable"


@dataclass
class Clarification:
    """One thing the question failed to pin down."""

    dimension: str
    ask: str
    options: list[str] = field(default_factory=list)
    why: str = ""


@dataclass
class Verdict:
    outcome: Outcome
    clarifications: list[Clarification] = field(default_factory=list)
    reason: str = ""
    detected_years: list[str] = field(default_factory=list)
    rule: str = ""

    @property
    def should_refuse(self) -> bool:
        return self.outcome is not Outcome.ANSWERABLE

    def as_message(self) -> str:
        """What the user actually sees when refused."""
        if self.outcome is Outcome.UNANSWERABLE:
            return f"I can't answer that: {self.reason}"
        if self.outcome is Outcome.AMBIGUOUS:
            lines = ["That question has more than one defensible answer here."]
            for c in self.clarifications:
                lines.append(f"\n  {c.ask}")
                for option in c.options:
                    lines.append(f"    - {option}")
                if c.why:
                    lines.append(f"    ({c.why})")
            return "\n".join(lines)
        return ""


# --- data availability, mirrored from fiscal_years -------------------------
# Kept as constants rather than queried per call so the rules stay readable;
# verified against the database by tests/test_ambiguity.py.
ALL_YEARS = [f"{y}-{str(y + 1)[2:]}" for y in range(2014, 2024)]
NO_ACTUALS = {"2022-23", "2023-24"}
NO_RE = {"2023-24"}
PLAN_ERA = {"2014-15", "2015-16", "2016-17"}

AMBIGUOUS_SCHEME_NAMES = {
    "swachh bharat": ["Swachh Bharat Mission - Gramin", "Swachh Bharat Mission - Urban"],
    "pmay": ["Pradhan Mantri Awaas Yojana - Gramin", "Pradhan Mantri Awaas Yojana - Urban"],
    "pradhan mantri awaas yojana": [
        "Pradhan Mantri Awaas Yojana - Gramin",
        "Pradhan Mantri Awaas Yojana - Urban",
    ],
    "awas yojana": [
        "Pradhan Mantri Awaas Yojana - Gramin",
        "Pradhan Mantri Awaas Yojana - Urban",
    ],
}

# Words that pin the estimate basis.
BASIS_WORDS = {
    "be": ("budget estimate", "budgeted", "be for", "as budgeted", "allocated in the budget"),
    "re": ("revised estimate", "revised", "re for"),
    "actual": ("actual", "actually spent", "actuals", "really spent", "was spent"),
}

# Words that pin the entity scope.
SCOPE_WORDS = {
    "all": ("total expenditure", "total spending", "overall", "including interest",
            "whole budget", "entire budget", "all spending", "all allocations",
            "counting all", "including those"),
    "administrative": ("ministries and departments", "administrative"),
    "ministries_only": ("only ministries", "ministries alone", "just ministries"),
}

# Question shapes that need an estimate basis at all.
MONEY_WORDS = (
    "spend", "spent", "spending", "expenditure", "budget", "allocat", "outlay",
    "receipt", "revenue", "transfer", "how much",
)

AGGREGATE_WORDS = ("total", "overall", "all ", "entire", "whole", "combined")

# Questions about receipts have no entity_scope dimension at all -- receipts are
# not booked to ministries -- so the scope rule must not fire on them.
RECEIPT_WORDS = ("receipt", "revenue", "tax", "borrowing", "devolution", "disinvest")

# A question that asks for a breakdown PER entity has already answered the
# scope question: it wants every entity, listed separately. "total" inside such
# a question describes the per-entity total, not a grand total.
PER_ENTITY_WORDS = (
    "each ministry", "by ministry", "per ministry", "each department",
    "by department", "per department", "each entity", "for each", "breakdown",
)

# Plan/Non-Plan has no Budget/Revised/Actual variant -- there is one column per
# classification, not three -- so the estimate-basis rule must not fire on it.
PLAN_WORDS = ("plan expenditure", "plan spending", "plan outlay", "non-plan", "non plan")

RENAMED_ENTITIES = {
    "education": [
        "Ministry of Human Resource Development (to 2019-20)",
        "Department of School Education and Literacy (from 2020-21)",
        "Department of Higher Education",
    ],
    "water": [
        "Ministry of Water Resources (to 2018-19)",
        "Department of Water Resources, River Development and Ganga Rejuvenation (from 2019-20)",
        "Department of Drinking Water and Sanitation",
    ],
}


def extract_years(question: str) -> list[str]:
    """Pull fiscal years out of a question in any of the usual forms."""
    found: list[str] = []
    text = question.lower()

    # '2019-20' or '2019-2020'
    for match in re.finditer(r"\b(20\d{2})\s*[-/]\s*(\d{2,4})\b", text):
        start = match.group(1)
        label = f"{start}-{str(int(start) + 1)[2:]}"
        if label in ALL_YEARS and label not in found:
            found.append(label)

    # bare '2019' -> the fiscal year beginning in 2019
    if not found:
        for match in re.finditer(r"\b(20\d{2})\b", text):
            year = int(match.group(1))
            label = f"{year}-{str(year + 1)[2:]}"
            if label in ALL_YEARS and label not in found:
                found.append(label)

    # 'FY20' -> 2019-20
    for match in re.finditer(r"\bfy\s*(\d{2})\b", text):
        end = 2000 + int(match.group(1))
        label = f"{end - 1}-{str(end)[2:]}"
        if label in ALL_YEARS and label not in found:
            found.append(label)

    return found


def _mentions(text: str, words) -> bool:
    return any(w in text for w in words)


def _basis_in(text: str) -> str | None:
    for basis, words in BASIS_WORDS.items():
        if _mentions(text, words):
            return basis
    return None


def _scope_in(text: str) -> str | None:
    for scope, words in SCOPE_WORDS.items():
        if _mentions(text, words):
            return scope
    return None


def assess(question: str) -> Verdict:
    """Decide whether a question can be answered, refused, or must be asked back."""
    text = " " + question.lower().strip() + " "
    years = extract_years(question)
    clarifications: list[Clarification] = []

    basis = _basis_in(text)
    is_money = _mentions(text, MONEY_WORDS)

    # --- UNANSWERABLE: Plan expenditure outside its era --------------------
    # Plan AND Non-Plan were abolished together from 2017-18, so both are
    # equally undefined afterwards. An earlier version of this rule excluded
    # "non-plan", which let "non-plan spending in 2020-21" through as
    # answerable -- the exact confidently-wrong shape the project exists to
    # catch.
    if _mentions(text, PLAN_WORDS):
        outside = [y for y in years if y not in PLAN_ERA]
        if outside:
            return Verdict(
                Outcome.UNANSWERABLE,
                reason=(
                    f"Plan expenditure does not exist for {', '.join(outside)}. "
                    "The Plan/Non-Plan classification was abolished from 2017-18 and "
                    "replaced by Scheme/Establishment. The successor figures are not "
                    "comparable to Plan expenditure, so substituting them would be "
                    "misleading rather than helpful."
                ),
                detected_years=years,
                rule="plan_era_boundary",
            )

    # --- UNANSWERABLE: actuals that do not exist yet -----------------------
    if basis == "actual" and years:
        missing = [y for y in years if y in NO_ACTUALS]
        if missing:
            return Verdict(
                Outcome.UNANSWERABLE,
                reason=(
                    f"Actual expenditure is not available for {', '.join(missing)}. "
                    "Actuals are published only after the accounts close, roughly two "
                    "years in arrears. Budget or Revised Estimates exist for those "
                    "years, but they are estimates, not what was spent."
                ),
                detected_years=years,
                rule="actuals_not_published",
            )

    if basis == "re" and years:
        missing = [y for y in years if y in NO_RE]
        if missing:
            return Verdict(
                Outcome.UNANSWERABLE,
                reason=(
                    f"Revised Estimates are not available for {', '.join(missing)}. "
                    "The revision is published with the following year's budget."
                ),
                detected_years=years,
                rule="re_not_published",
            )

    # --- AMBIGUOUS: scheme names that match more than one certified scheme --
    for name, options in AMBIGUOUS_SCHEME_NAMES.items():
        if name in text and not any(o.lower() in text for o in options):
            clarifications.append(
                Clarification(
                    dimension="scheme_identity",
                    ask=f'"{name.title()}" matches more than one scheme:',
                    options=options,
                    why="different schemes, different ministries, different money",
                )
            )
            break

    # --- AMBIGUOUS: estimate basis not pinned ------------------------------
    # Plan/Non-Plan figures have a single column each, so there is no basis to
    # pin. Asking "Budget or Revised?" about Plan expenditure is a question
    # with no answer, and refusing on it would be a false refusal.
    is_plan_question = _mentions(text, PLAN_WORDS)
    if is_money and basis is None and not is_plan_question:
        clarifications.append(
            Clarification(
                dimension="estimate_basis",
                ask="Which figure do you mean?",
                options=[
                    "Budget Estimate - what was proposed when the budget was presented",
                    "Revised Estimate - the mid-year correction",
                    "Actuals - what was really spent (available to 2021-22 only)",
                ],
                why="these are three different numbers, routinely 10-20% apart",
            )
        )

    # --- AMBIGUOUS: entity scope not pinned on an aggregate question -------
    wants_aggregate = _mentions(text, AGGREGATE_WORDS) and is_money
    names_entity = _mentions(text, ("ministry of", "department of", "scheme"))
    about_receipts = _mentions(text, RECEIPT_WORDS)
    per_entity = _mentions(text, PER_ENTITY_WORDS)
    if (
        wants_aggregate
        and not names_entity
        and not about_receipts
        and not per_entity
        and not is_plan_question
        and _scope_in(text) is None
    ):
        clarifications.append(
            Clarification(
                dimension="entity_scope",
                ask="Which spending should that cover?",
                options=[
                    "All spending, including Interest Payments, Defence Pensions "
                    "and Transfers to States",
                    "Ministries and departments only, excluding those functional lines",
                ],
                why="the functional lines are ~44% of the budget; the two differ about fivefold",
            )
        )

    # --- AMBIGUOUS: an entity that was renamed, over a span crossing it ----
    for key, options in RENAMED_ENTITIES.items():
        if key in text and _mentions(text, ("ministry", "department", "spending on")):
            crosses = len(years) > 1 or _mentions(text, ("since", "over time", "trend", "each year", "across"))
            if crosses and not any(o.split(" (")[0].lower() in text for o in options):
                clarifications.append(
                    Clarification(
                        dimension="entity_identity",
                        ask=f'Which "{key}" entity do you mean?',
                        options=options,
                        why="the entity was renamed mid-series; the same money has two names",
                    )
                )
            break

    if clarifications:
        return Verdict(
            Outcome.AMBIGUOUS,
            clarifications=clarifications,
            detected_years=years,
            rule="+".join(c.dimension for c in clarifications),
        )

    return Verdict(Outcome.ANSWERABLE, detected_years=years, rule="none")
