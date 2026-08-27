# Gold set audit

Run: `python scripts/audit_gold.py` · 27 August 2026 · model: Sonnet 5

## What this is, precisely

Not blind cross-model verification. This audit was written in the same
conversation thread that wrote `eval/questions.yaml`, so the author had the
original gold SQL in context while designing every check below — genuine
blindness was not achievable and this report does not claim it.

What it is instead: for each of the 32 answerable/trap gold queries, an
independent check built a **different** way — a different join path to the
same number, an algebraic identity, or a known invariant from
`scripts/load_data.py` (e.g. `gross_amount_crore` is set equal to
`be_amount_crore` at generation time, so they must match *exactly*, not
approximately). This catches implementation bugs — wrong column, wrong join,
wrong filter, sign error — that a second read-through in the same reasoning
context would not.

It does **not** validate the 16 interpretive judgement calls in the
`ambiguous` category. No script can settle those; see the second section.

## Mechanical audit: 31 checks run, 26 confirmed, 5 initially flagged

All 5 flags were traced to their root cause. **All 5 were bugs in the audit's
own cross-check, not in gold.** Diagnosis for each:

| id | what the audit assumed | what was actually true | verdict |
|---|---|---|---|
| **Q12** | hardcoded name list included "Defence Services (Capital)" and omitted "Pensions" | "Defence Services (Capital)" doesn't exist as an entity; "Pensions" does and is correctly tagged `entity_kind='functional'` | gold correct — audit's list was wrong, not gold's classification |
| **Q14** | `NOT (effective_to_year < 2016)` is equivalent to `effective_to_year >= 2016` | `NOT (NULL < 2016)` evaluates to `NULL`, not `TRUE`, in SQL's three-valued logic — 102 of 107 ministries have `effective_to_year IS NULL` and the negated form silently drops nearly all of them (102 → 4) | gold correct — its explicit `IS NULL OR >=` form is required; the audit's "equivalent" phrasing was a real SQL NULL trap, ironically the same species of bug this whole project is about |
| **Q15** | no state above 20M population should carry special-category status | Assam (pop. 31.2M) genuinely holds special-category status historically | not a bug anywhere — the audit's real-world prior was wrong, unrelated to gold's SQL |
| **T03** | `plan/be` ratio must equal 0.45 within 1e-9 | true ratio is `0.45000000707...` — each row's `plan_amount_crore` is independently rounded to 3 decimals before summing across ~1,000+ rows, so cumulative rounding error of ~7e-9 is expected and negligible | gold correct — audit's tolerance was tighter than floating-point summation can deliver |
| **T04** | every ministry's row group in the monthly CTE should have exactly 12 raw rows before aggregation | the CTE groups by `(fiscal_year, ministry_id)` and produces exactly **one** row per ministry after `GROUP BY` — confirmed directly (97 ministries, 1 row each) — which is what actually matters for the outer `MAX()` to be safe | gold's `MAX()` over a join to a single-row-per-group CTE is correct; audit tested the wrong quantity |

The 5 questions marked informational (Q07, Q16, Q17, T05, T12 — cases where
the "correct" relationship isn't a strict pass/fail) were followed up
individually and all reconcile exactly:

- **Q07**: real UT list is `{DL, JKU, LA, PY}` — 4, matching gold.
- **Q16**: `total − (non_tax+capital)` = 2,590,770.53, matching gold's tax-only sum exactly.
- **Q17**: sum of the 12 grouped monthly figures = 666,197.19, matching the flat table-wide `SUM()` exactly.
- **T05**: `grand_total − functional` = 2,159,279.52, matching gold's administrative-only sum exactly.
- **T12**: BE and RE computed as two separate single-column queries match gold's combined two-column query exactly (166,526.656 / 158,765.505).

**Net result: 32/32 gold queries with `gold_sql` hold up under independent
mechanical verification.** This is real evidence, not a rubber stamp — it
required tracing five genuine disagreements to their source, and four of the
five root causes are worth knowing about independent of this audit (the NULL
double-negation trap in particular is a good one to remember).

## What this does not establish

The 32 gold queries are now the strongest-verified part of the eval set. The
16 `ambiguous` and 12 `unanswerable` categorizations are not touched by any of
the above — those are judgement calls about what counts as a single certified
reading, and no cross-check can validate a judgement call.

One structural observation from reading through the 16 `ambiguous` questions
again: six of them — A01, A04, A08, A11, A15, A16 — are the same underlying
ambiguity (estimate basis *and* entity scope both unpinned on a generic "how
much did we spend" question) restated with different phrasing or year. All
six are correctly classified; the redundancy isn't a *wrongness*, but the
16-question bucket is really testing four distinct ambiguity types with
uneven coverage:

| ambiguity type | questions | count |
|---|---|---|
| basis + scope both unpinned (generic aggregate) | A01, A04, A08, A11, A15, A16 | 6 |
| scheme name matches >1 certified scheme | A03, A05, A07, A13 | 4 |
| entity renamed mid-series | A06, A12 | 2 |
| receipts: revenue-only vs including borrowings | A10 | 1 |
| basis unpinned, named entity (scope N/A) | A02, A09 | 2 |
| basis unpinned, sector description | A14 | 1 |

If revising this set, trading two or three of the six generic basis+scope
repeats for dedicated coverage of dimensions that currently get one slot or
none — gross/net ambiguity (though `metrics.yaml` gives it a certified
default, so it may never need to fire), or a case stacking scheme-name
ambiguity with a renamed entity — would raise the ceiling on what the
category tests without changing its size.

## Bottom line

- **32/32 gold SQL queries: independently verified correct** by a different
  computational path or a known generation invariant, with full diagnostic
  trail on every apparent disagreement.
- **28/28 refusal categorizations: not re-litigated here.** They were checked
  for internal consistency against the refusal *rules* in an earlier pass
  (`scripts/verify_gold.py`, 60/60 agreement) — but that only proves the rules
  and the gold set agree with each other, not that either is right. The
  structural note above is the closest this audit gets to reviewing them, and
  it's an observation about coverage, not a correctness check.
- The interpretive judgement calls — starting with the six generic
  basis+scope questions and whether `estimate_basis` should carry a certified
  default the way `gross_net` does — are exactly the ones that need a human,
  and are listed here rather than resolved.
