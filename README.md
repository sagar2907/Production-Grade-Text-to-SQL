# Rosetta

**Text-to-SQL that refuses to guess — and measures how often it used to.**

<!-- RESULTS-START -->
> **Confidently wrong answers fell from 77% to 17%** on a 60-question evaluation set — identical model, identical schema, semantic layer switched off and on.
>
> Measured with `ollama:qwen2.5-coder:7b`. 28 of the 60 questions have no single certified answer and should be refused rather than answered.
<!-- RESULTS-END -->

## What that looks like

One ambiguous question, same model, same schema, run twice — the only
difference is whether `semantic/` is loaded. Transcript is captured from an
actual run by `scripts/capture_demo.py`, not written by hand:

<!-- DEMO-START -->
```
==========================================================================
  ROSETTA -- the same question, the same model, twice
==========================================================================

  Question: How much did the government spend in 2019-20?
  Model:    ollama:qwen2.5-coder:7b

==========================================================================
  WITHOUT the semantic layer
==========================================================================
  SQL:
    SELECT SUM(expenditure_crore) AS total_expenditure FROM expenditure_actuals WHERE fiscal_year = '2019-20'

  Result:
    666,197

  No caveat. No question asked. The user has no way to know
  which of the three estimate bases this is, or whether it
  includes Interest Payments.

==========================================================================
  WITH the semantic layer
==========================================================================
  That question has more than one defensible answer here.

    Which figure do you mean?
      - Budget Estimate - what was proposed when the budget was presented
      - Revised Estimate - the mid-year correction
      - Actuals - what was really spent (available to 2021-22 only)
      (these are three different numbers, routinely 10-20% apart)

==========================================================================
  What the certified readings actually return
==========================================================================
  Budget Estimate, all spending                             2,905,387
  Budget Estimate, ministries and departments only          1,636,754
  Revised Estimate, all spending                            2,872,216
  Actuals, all spending                                     2,643,743

  4 defensible answers to one question, spanning a 1.8x range.
  Picking one silently is the failure this project measures.

  Note: the baseline answered 666,197, which is not any of these.
  Its nearest certified reading is 1,636,754 (Budget Estimate, ministries and departments only),
  which it is off by 59%.
  It did not pick the wrong reading. It produced a fifth number,
  with no indication that it had.
```
<!-- DEMO-END -->

---

## The problem

The same model scores **91.2% on Spider 1.0 and 21.3% on Spider 2.0**. Same model, same task, a 70-point collapse. On BEAVER — real warehouse data — off-the-shelf models approach ~0%.

The gap is not model quality. Spider 1.0 databases have a handful of tables; Spider 2.0 averages ~800 columns. The named causes are all *context* problems: ambiguous column names, undocumented join paths held as tribal knowledge, access control that benchmarks ignore by running as superuser, and dialect divergence. The consensus fix is **fix the context, not the model**.

Every text-to-SQL portfolio project is built on the 91% side of that cliff — eight clean tables, one dialect, "it worked when I tried it." This one is built on the 21% side.

And it is built around the failure that actually does damage. Most evaluations collapse everything into execution accuracy, which treats these two as equivalent:

```
the query errored           the user sees an error and asks again
the query returned 8,412    the user puts 8,412 in a report
```

They are not equivalent. The second is the one nobody measures.

## What it does

Ask a question in English. The system does one of three things:

- **Answers it**, showing the SQL.
- **Asks back**, if the question has more than one certified reading. "How much did the government spend in 2019-20?" is not one question — Budget Estimate, Revised Estimate and Actuals are three different numbers, and including or excluding Interest Payments changes the answer fivefold.
- **Declines**, if no reading has data. "What was actually spent in 2023-24?" has no honest answer — actuals are published about two years in arrears. Returning `0` would be a confidently wrong answer, because zero is a number a reader will believe.

The moat is not the prompt. It is `semantic/` — a versioned column glossary, certified metric definitions, and an allow-list of validated join paths. The ablation runs the identical model against the identical schema with that directory switched off.

## The schema

Modelled on the Indian Union Budget: **11 tables, 182 columns, 10 fiscal years**.

Six ambiguities are designed in, each modelled on a real property of the published data. All six are checked by `scripts/verify_gold.py` on every build, so the numbers below are measured rather than asserted:

| Ambiguity | What goes wrong |
|---|---|
| **Units** | `be_amount_lakh` is exactly 100× `be_amount_crore`. Sum the wrong one and you are 100× out. Runs cleanly, returns a plausible budget figure. |
| **BE / RE / Actual** | Three different numbers for the same year, routinely 10–20% apart. "The budget" picks none of them. |
| **Plan / Non-Plan** | Abolished from FY2017-18. The columns are populated for 3 years and NULL for 7. `SUM()` across all years silently reports three years as if it were ten. |
| **Entity granularity** | Interest Payments, Defence Pensions and Transfers to States are real spending lines but not administrative bodies — ~44% of the budget. "Total expenditure" ₹39.4L cr vs "what ministries spent" ₹7.5L cr — **5.2× apart**. |
| **Renames** | Ministry of Human Resource Development (to 2019-20) and Department of School Education and Literacy (from 2020-21) are the same money. Filtering one name shows a spending cliff that never happened. |
| **Grain / fan-out** | `budget_allocations` is annual, `expenditure_actuals` is monthly. Joining without aggregating first inflates the total **35×**. |

## Provenance — read this before quoting any figure

This matters more than anything else in the README, so it is not in a footnote.

**Real.** 102 spending-entity names, their allocations, and the BE/RE/Actual relationship come from the published Union Budget reference file (`data/raw/`, loaded by `scripts/reference.py`). Magnitudes span five orders of magnitude, ₹11.61 crore to ₹4.77 lakh crore. The database's 2023-24 total reconciles to within ~2% of the real grand total scaled back one year.

**Generated.** The detail *beneath* each entity — scheme, account-head, state and month level rows — and the years the reference file does not cover. Generated detail is normalised so it sums back to the real entity anchor.

**Historical.** Five ministries renamed or merged before the reference file begins, added on their real years.

So: **the structure and the magnitudes are real; the row-level detail is synthetic.** Do not describe this as "real government data" without that qualification.

> **A note on the source.** `openbudgetsindia.org`, the obvious place to get this data, is dead — as of August 2026 the domain serves an unrelated commercial site. The surviving copy is a GitHub mirror. A reviewer who tries the obvious source will not find what you found.

Two things the real file taught the schema, which I would not have invented:

1. **`Grand Total` is a row in the published file**, sitting alongside the components that make it up. Summing the column returns ₹95,31,537 crore against a true ₹47,65,768 crore — *exactly double*, cleanly, plausibly.
2. **The budget is organised by department, not ministry.** There is no "Ministry of Education" line; the money appears under Department of School Education and Literacy. That granularity shift is why "the education ministry's budget" needs a certified reading.

## The evaluation set

`eval/questions.yaml` — 60 questions in four categories:

| Category | n | Correct behaviour |
|---|---|---|
| `answerable` | 20 | answer, matching gold SQL |
| `trap` | 12 | answer, resisting a tempting wrong path that runs cleanly |
| `ambiguous` | 16 | **refuse** — more than one certified reading |
| `unanswerable` | 12 | **refuse** — no reading has data |

> ⚠️ **The gold set was drafted by Claude and needs human review.** Every gold query executes, which is a much weaker claim than every gold query being *right*. A wrong gold query does not error — it silently makes the headline number wrong. `scripts/verify_gold.py` checks that all 32 gold queries run, flags suspicious result shapes, and cross-checks that the refusal rules and the gold set agree on all 60 questions. It cannot check whether a judgement call was the right one.

## Outcome taxonomy

```
CORRECT             answered, result matches gold
CONFIDENTLY_WRONG   returned a clean, plausible, WRONG number     <- the headline
WRONG_ORDER         right values, wrong sequence
ERRORED             failed after repair; visible, therefore safe
REFUSED_RIGHTLY     declined a question with no single answer
REFUSED_WRONGLY     declined a question it should have answered
```

`CONFIDENTLY_WRONG` counts two things: answering an answerable question with the wrong rows, **and** producing a number for a question that should have been refused. The second counts even if the figure matches one valid reading, because the user has no way to know which reading they got.

`WRONG_ORDER` is deliberately **not** folded into the headline. A reader who gets the right rows in a different sequence has not been misled about any figure, and counting it as confidently wrong would inflate the result in this project's own favour — the one thing an evaluation about wrong numbers cannot do.

`REFUSED_WRONGLY` is tracked because a system that refuses everything scores zero confidently-wrong while being useless. The pair must be read together.

## Architecture

```
question
   |
   v
ambiguity.assess()          rule-based, deterministic, before any model call
   |
   +-- AMBIGUOUS ---------> ask back, listing the certified readings
   +-- UNANSWERABLE ------> say why; do NOT return zero
   |
   v  ANSWERABLE
generate ---> EXPLAIN ---> execute ---> repair (max 3)
   |            ^                          |
   |            +--------------------------+
   v
answer + the SQL, always shown
```

Refusal is deliberately **rule-based, not model-based**: it has to be deterministic (or the headline number moves for reasons unrelated to what is being measured), auditable (every refusal traces to a named rule), and cheap (it runs before generation).

`EXPLAIN` is the cheap gate — it catches unknown columns and bad joins for the price of a plan, before the query spends its execution timeout.

## Run it

```bash
pip install duckdb sqlglot pandas pyyaml
```

```bash
python scripts/load_data.py && python scripts/verify_gold.py
```

```bash
python scripts/run_eval.py --model qwen2.5-coder:7b
```

Needs a local [Ollama](https://ollama.com) server. `qwen2.5-coder:7b` fits in ~6 GB VRAM. The generator is pluggable (`src/rosetta/generate.py`) — local inference was chosen because the eval loop makes 500+ model calls per pass and a free API tier would throttle every run.

## Results

<!-- ABLATION-START -->
Model: `ollama:qwen2.5-coder:7b` · n = 60 · 502s total

| metric | baseline | semantic | delta |
|---|---:|---:|---:|
| confidently wrong | 76.7% | **16.7%** | -60.0 pts ✓ |
| execution accuracy | 34.4% | **53.1%** | +18.7 pts ✓ |
| refusal recall | 0.0% | **100.0%** | +100.0 pts ✓ |
| refusal precision | 0.0% | **100.0%** | +100.0 pts ✓ |

Outcome counts:

| outcome | baseline | semantic |
|---|---:|---:|
| `correct` | 11 | 17 |
| `confidently_wrong` | 46 | 10 |
| `wrong_order` | 1 | 4 |
| `errored` | 2 | 1 |
| `refused_rightly` | 0 | 28 |

Per trap (correct / total):

| trap | baseline | semantic |
|---|---:|---:|
| borrowings | 0/1 | 1/1 |
| devolution | 0/1 | 0/1 |
| entity_scope | 0/1 | 0/1 |
| estimate_basis | 0/1 | 0/1 |
| fan_out | 0/1 | 0/1 |
| gross_net | 1/1 | 1/1 |
| null_join | 1/1 | 1/1 |
| plan_era | 0/1 | 1/1 |
| rename | 0/1 | 0/1 |
| scheme_merge | 0/1 | 0/1 |
| units | 1/2 | 2/2 |

`wrong_order` is tracked separately from `confidently_wrong`: right values in the wrong sequence has not misled anyone about a figure, and folding it into the headline would inflate the result in this project's favour.
<!-- ABLATION-END -->

## How much of that is noise?

A headline number quoted without its spread is not a falsifiable claim, so the
harness was measured against itself: each arm run three times, same model, same
questions, temperature 0. `python scripts/measure_variance.py --repeats 3`

| | baseline | semantic |
|---|---|---|
| confidently wrong | 76.7% every run, **0.0 pts spread** | 16.7% every run, **0.0 pts spread** |
| execution accuracy | 34.4% every run, 0.0 pts spread | 53.1–56.2%, 3.1 pts spread |
| questions with an unstable outcome | **0 of 60** | **1 of 60** (Q05, flapping `correct` ↔ `wrong_order`) |

**Effect 60.0 pts against noise 0.0–3.1 pts.** The headline is not a lucky run.

Two things worth stating precisely, because they cut in opposite directions:

- *Within* a single `ollama serve` session the eval is effectively
  deterministic — one borderline question moves, nothing else.
- *Across* a server restart it shifts by about one question (an earlier run
  recorded a 75.0% baseline where these three all record 76.7%). GPU batching
  and KV-cache state differ on a cold start. So results are reproducible within
  a session and approximately reproducible across them, and any claimed
  improvement smaller than ~2 questions should not be believed.

That second point is not hypothetical. It caught a real error in this project:
an expansion of the semantic layer appeared to improve execution accuracy until
the variance run showed the movement was inside the spread. It fixed two
specific behaviours (see below) without moving the headline at all, and is
reported that way rather than as a gain.

## Two generators, same harness

The same 60 questions, the same schema, the same semantic layer — two different models.

| | qwen2.5-coder:7b | llama3.1:8b |
|---|---:|---:|
| | *code model* | *general model* |
| confidently wrong, baseline | 76.7% | 76.7% |
| confidently wrong, semantic | **16.7%** | **35.0%** |
| delta | −60.0 pts | −41.7 pts |
| execution accuracy, semantic | 53.1% | 18.8% |
| refusal recall, semantic | 100% | 100% |
| first attempt ran | 94% | 72% |
| runtime, both arms | 502 s | 647 s |

Three things fall out of this, and the third is the one worth arguing about.

**The baseline rate is model-independent.** Both models score **76.7%** — identical, not merely close. Swapping a general model for a code model, which is worth 34 points of execution accuracy once the layer is on, moves the baseline confidently-wrong rate by exactly nothing. The rate is a property of the schema's ambiguity, not of the model's competence, and a better model does not rescue you from it.

**Refusal is entirely model-independent.** Both models hit 100% refusal recall, because refusal is a rule that runs *before* generation. 28 of the 60 questions never reach a model at all. That is the design working as intended: the decision not to answer is not delegated to the thing being guarded against.

**But the layer does not close the whole gap.** After the semantic layer, the two models are still 18 points apart on confidently-wrong and 34 points apart on execution accuracy. So the honest version of "fix the context, not the model" is narrower than the slogan: *context fixes the ambiguity half; model capability still owns the correctness half.* The semantic layer stops the system answering questions it should not answer. It does not make a weak model write good SQL.

## What the semantic layer could not fix

Reading the failures the layer *doesn't* catch turned out to be more useful
than the headline. Three were systematic, and all three are things a domain
expert would write down before ever seeing an eval result — so they were added
to the layer:

| failure | cause | fix |
|---|---|---|
| `WHERE status = 'Closed'` | the value is `'closed'` — the model **guessed** an enum literal, and a wrong literal matches nothing while still returning cleanly | exact value domains, read live from the database by `value_domains()` so they cannot go stale |
| `WHERE effective_to_year >= 2016` | silently drops every still-active entity, because `NULL >= 2016` is `NULL`, not `TRUE` | an explicit NULL-semantics section: which columns use NULL as a *meaning* rather than an absence |
| joined the `departments` table | "Department of Higher Education" is a row in **`ministries`**; `departments` holds internal sub-units like "Secretariat, MoR" | a "where entities live" note naming the confusing table |

**And then the honest part: this did not move the headline.** Confidently-wrong
stayed at exactly 16.7% before and after. The enum fix landed cleanly and the
NULL fix landed partially (the model adopted the NULL guard, then dropped a
different condition); other questions moved the other way and the net was zero,
inside the measured noise.

It is reported here as a *behavioural* improvement with **no measured effect on
the headline**, because that is what the variance run showed. The tempting
version — quoting the run where execution accuracy read 56.2% instead of the
53.1% three consecutive runs produced — would have been a 3-point gain invented
out of a cold GPU cache.

## Layout

```
data/schema.sql          11 tables, 182 columns, ambiguities documented inline
scripts/reference.py     parses the real published budget file
scripts/load_data.py     builds the database, deterministic from one seed
scripts/verify_gold.py   gold SQL + refusal-rule agreement + trap checks
scripts/audit_gold.py    independent verification of all 32 gold queries
scripts/run_eval.py      both ablation arms
scripts/measure_variance.py  how much of the result is harness noise
scripts/capture_demo.py  regenerates the transcript above from a real run
semantic/glossary.yaml   what each ambiguous column actually means
semantic/metrics.yaml    certified metrics and the dimensions they require
semantic/joins.yaml      certified / ambiguous / forbidden join paths
eval/questions.yaml      the 60-question gold set   <- review this first
eval/gold_audit_report.md  what the gold audit verified, and what it did not
src/rosetta/db.py        read-only sandbox, parse-verified, timeouts
src/rosetta/compare.py   result-set normalisation
src/rosetta/ambiguity.py refusal rules
src/rosetta/correct.py   generate -> EXPLAIN -> execute -> repair
src/rosetta/evaluate.py  the outcome taxonomy and the ablation
```

## Known limitations

- **The 28 refusal categorizations are unreviewed.** The 32 gold SQL queries
  are independently verified (`scripts/audit_gold.py`, 32/32); the judgement
  calls about which questions *should* be refused are not, and no script can
  settle them. This is the largest open item. See `eval/gold_audit_report.md`,
  which also notes that six of the sixteen `ambiguous` questions test the same
  underlying ambiguity reworded.
- **Row-level detail is synthetic.** Structure and magnitudes are real; the detail is not.
- **Single dialect.** DuckDB only. Multi-dialect compilation was cut for scope.
- **No row-level security.** Every query runs as the same read-only role. Real warehouses enforce per-user visibility and benchmarks that ignore it overstate accuracy.
- **Ambiguity rules are keyword-based.** They are deterministic and auditable, which is the trade being made, but they will miss paraphrases a model would catch. The 100% refusal recall reported above is against *this* question set; a set written by someone else would score lower.
- **Results are session-reproducible, not bit-reproducible.** Identical within one `ollama serve` session; about one question of drift across a restart. See the variance section.
