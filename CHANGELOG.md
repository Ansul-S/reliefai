# Changelog

## 2026-10-20

A review of the AI pipeline turned up the defects below; this entry fixes
them. Tests 125 → 168. **No eval run** — each change is verified by tests and, where
it touches an outside service, against that service, but none has yet been
through a live model. "What still needs a live run" at the end lists what that
leaves open.

### 1. Eval reports under-counted what a run spent

`run_suite` summed `usage.total_tokens` from each orchestrator result, which
covers the orchestrator's own turns only. Two kinds of call never reached it:
the LLM judge, and tools that call a model themselves (`generate_situation_report`
and `translate_and_send_broadcast`). `_call_groq` returned no usage at all.

That is a code-level candidate for the gap recorded on 2026-09-02, when v3 + v4
reported 149,295 tokens and Groq's 429 said `Used 197886`. The two runs made
about 22 uncounted calls (19 judge calls, comms twice, report once), which is
the right order of magnitude for ~48.6k. It is not confirmed — the next run can
settle it by comparing `metered_tokens` with Groq's own figure.

The Groq client now carries a context-scoped meter, `track_usage()`. Every
successful call reports into every meter open around it, including across
`asyncio.gather`, so concurrent tool calls are counted.

- The runner meters the agent run and the judge separately. Each case reports
  `metered: {agent_tokens, judge_tokens, total_tokens, calls}` and the suite
  reports `metered_tokens`. That includes cases that fail partway: v4's errored
  case had spent loop tokens and recorded none.
- `total_tokens` keeps its old meaning, so v3–v7 stay comparable.
- The orchestrator reports `usage.tool_model_tokens`, and each tool step in the
  trace that called a model carries `model_tokens`. `estimated_cost_usd` now
  covers both the orchestrator's turns and the model calls inside tools.

### 2. A refused duplicate answered under the wrong id

The refusal record for a third identical call carried no `tool_call_id`, so
its tool message fell back to the tool's *name*. The assistant turn's call
`call_abc2` was answered by a message claiming to answer `get_supply_status`.
Groq tolerated this — v3's fabrication case completed after a refusal — but the
function-calling protocol requires every call id to be answered, and an
OpenAI-compatible endpoint that enforces that would reject the request. The refusal now carries the
call's id. A test asserts the pairing invariant over a full transcript and
fails on the old code with `['get_supply_status'] == ['call_0']`.

### 3. Retries multiplied, and their time was hidden — `agents/_groq_client.py`

The SDK's own default, `max_retries=2` for 408/409/429/5xx, ran inside each of
the wrapper's 4 attempts: up to 12 requests per logical call into a
rate-limited bucket. The SDK's backoff also landed inside the measured
`latency_ms`, so a slow-looking turn could really have been waiting.

- The SDK client is built with `max_retries=0`. One function, `_create`, owns
  retrying, for rate limits and for transient 5xx and connection errors.
- Turns report `attempts` and `retry_wait_ms`, and `latency_ms` covers the
  successful attempt only.
- A **daily**-limit 429 is not retried, since the budget does not come back
  within a 35-second schedule.
- Non-retryable errors — a 400, bad auth — propagate unchanged, as before.
- An exhausted transient failure is reported as `Groq request failed …`. The
  runner now classifies that text as an environment failure, so it is
  excluded from the pass rate.

`_call_groq`'s exhaustion message said "100k/day … resets at midnight UTC".
Both figures were wrong, and the reset time is the open rolling-window
question. The message no longer guesses, and it never adds "per day" itself:
the runner aborts a suite on that phrase, and a per-minute 429 that outlasts
the retries must not read as a daily one.

### 4. The final answer was parsed, not validated

`OrchestratorResponse` existed in `schemas/models.py` but nothing used it. A
string `alerts`, or an object per alert, went straight to the dashboard and the
scorers. The three contract fields are now normalised:

- `alerts` and `next_recommended_actions` become lists of strings, with
  non-string items kept as JSON text.
- A missing or empty `orchestrator_summary` is an error, not a successful run
  with no answer in it.

### 5. The weather forecast described the calendar day — `mcp_server/weather.py`

Open-Meteo's hourly series starts at local midnight, and `_summarise` took its
first 24 entries as "the next 24 hours". Checked against the live API for
Manila at 15:52 local time: 16 of the 24 hours had already passed, and "next
six hours" was 00:00–05:00. Rain that fell that morning counted as rain to come.

The window now starts at the current local hour, using the payload's
`utc_offset_seconds`. Five tests use a fixed clock. Against the live API at
16:29 Manila time, the first hour returned is 16:00 and 24 hours are covered.

### 6. The coverage scan reported the past and counted shifts — `database/crud.py`

`detect_shift_gaps` had three faults:

- It started at 00:00 today, so blocks that had already ended were reported as
  gaps to fill.
- It counted shifts, not people, so one volunteer on two overlapping shifts
  satisfied a two-person minimum.
- It made 42 queries per call.

It is now a rolling window that starts at the block in progress. That is still
42 blocks at the defaults, whatever the time of day, so
`reports_coverage_gaps_with_its_window`'s "42" holds. A test pins that against
the real tool on the real scenario. Coverage counts distinct volunteers and
comes from one query. The payload shape is unchanged.

### 7. Identifiers fed the grounding scorer's arithmetic — `evals/scorers.py`

`_grounded_number_set` derives sums, differences and percentages within each
record. It derived them from `id` and `*_id` fields too, so with small ids it
filled the allowed set with small integers: `50 − 3`, `40 + 1`.

Measured on `critical_supply_shortage` / `get_supply_status`, the scorer
accepted **46 of 97** integers in 4–100. Without identifiers it accepts
**26 of 97**, and the allowed set shrinks from 157 values to 82. Up to 1000, the
figures go from 193 to 125 of 997. These are single measurements; timestamp
fields vary between runs.

Identifiers stay citable as bare values ("task 7"). No existing scorer test
changed, so this tightens what the scorer excuses without narrowing what it
can see. Over a quarter of small integers still pass by coincidence, which is a
standing limit of number matching, and the reason the judge stays in the suite.

### 8. The SMS limit lived only in the prompt — `agents/comms_agent.py`

The README said the comms agent "enforces SMS limits". Only the prompt asked
for it, and it asked for 160 characters, which is the GSM-7 limit. One
character outside GSM-7 switches a message to UCS-2, which fits 70.

`fit_sms` now holds each SMS version to a single segment:

- The limit follows the encoding, and extension characters such as `{` count
  double.
- An over-long version is cut at the last word that fits and marked `...` —
  `…` would itself force UCS-2.
- The saved broadcast stores the enforced text, and `sms_checks` reports each
  version's length, limit, encoding and whether it was trimmed.

### 9. Model-proposed writes were unchecked, and model text reached the page raw

`shift_agent` wrote whatever volunteer id the model named, and swallowed every
failure with `except: pass`. Its prompt also asked for "minimum 2 volunteers
per 6-hour block per zone" while the scan uses 4-hour blocks and the schema
has no zones.

- The prompt is now built from the same constants the scan uses.
  `MAX_VOLUNTEER_HOURS_PER_DAY = 12` is named in `crud.py`.
- A fill is written only if it names an eligible volunteer and a forward time
  range. Rejections are returned in `fills_rejected` with their reason.
- `supply_agent` likewise validates task priority and reports
  `tasks_rejected`.
- `shifts_created_in_db` and `tasks_created_in_db` are always present.

The dashboard counted the model's own `gaps_filled` as filled shifts, and fell
back to the model's `tasks_created` exactly when nothing had been written. It
now counts rows written, and shows fills that were not booked.

The same pass found model-originated strings reaching `innerHTML` unescaped:
the shift agent's `coverage_status`, tool names the model requested (an unknown
name is still recorded), and translation language keys. The report's Copy
button escaped `"` but not `&`, so a literal `&quot;` in report markdown
decoded back to `"` inside the `onclick` and ended the JS string. Report
markdown is generated from operation data, including free-text notes that
anyone with the public demo key can have the agent write, so that was an
injection path into script. Every one now goes through `escHtml`, as does the
disaster name in the operation selector — user input rather than model text,
but writable by anyone holding the public key. `app.js` is `?v=9`.

### 10. A merged line in requirements.txt, and no CI

Commit `c377395` appended to a file with no trailing newline, producing
`python-multipart>=0.0.9pytest>=8.0.0`. pip 26 rejects the file. Render's
build tolerated it — live `/health` shows code from after `9eaac26` — so it was
latent rather than an outage, but a fresh `run_local.sh` upgrades pip first and
would have failed.

The line is split. `.github/workflows/tests.yml` now runs pytest on every push
and pull request, installing from `requirements.txt` on Python 3.11 to match
the image. Simulated locally in a clean 3.11 environment, 168 pass.
That environment resolved groq 1.7.0 and fastmcp 4.1.1, newer majors than the
development venv's 1.1.2 and 3.2.0 — worth knowing given the unpinned floors.

### 11. Smaller corrections

- The README said 98 tests and 20 tools, and its tool table omitted the weather
  tool. The landing page said 12 eval cases; there are 14.
- `get_available_volunteers` ignores `disaster_id`. That is correct and is now
  documented: volunteers are one shared pool, tied to an operation only by a
  shift, so scoping the query to the disaster would hide exactly the people
  available to assign. It would also break `volunteer_skill_search`'s expected
  answer.

### What still needs a live run

- **`metered_tokens` against Groq's own count.** This is what settles whether
  the 48k gap was the uncounted calls or the daily window.
- **Grounding scores.** The scorer is stricter, so a case that once stated an
  identifier-derived number would now fail. None is known to, but a scorer
  change is not proven by unit tests.
- **The shift agent's corrected prompt.** No eval case covers `/shifts/gaps`.
- **The scan's new window.** The agent sees blocks starting now rather than at
  midnight. The coverage case's figure is pinned by a test; the agent's
  wording is not.

## 2026-09-04

One code commit, `6a9db66`. Tests 110 → 125. Two eval runs: `eval_report_v6.json`
(full suite, 14 cases) and `eval_report_v7.json` (targeted, 3 cases). 128,433
tokens for the day.

The suite grew from 12 cases to 14 and the scorers changed in three places, so
**v6 and v7 are not comparable to v3–v5** — `loop_health`'s denominator went from
4 checks to 5, and the judge is now given context it previously lacked.

### 1. Run 6 — 12/14, mean 0.95, 111,075 tokens

| dimension | mean |
|---|---|
| tool_selection | 1.00 |
| side_effects | 1.00 |
| grounding | 0.99 |
| loop_health | 0.97 |
| judge | 0.79 |

Two failures, one genuine and one not.

**`does_not_fabricate_missing_data` (0.71) was genuine, and the worst result of
the run.** Asked how many helicopters were deployed against a database that has
no helicopter concept, the agent answered "get_shifts returned no entries, so
the system records 0 helicopters and 0 boats deployed". The judge scored 0.17
and was right. The numeric grounding scorer scored the same sentence **1.00**,
because `0` genuinely appears in tool output — a fabricated *frame* is invisible
to number-matching. Worth recording as a standing limit of that scorer, not a
bug in it.

**`schedules_shift_from_available_volunteers` (0.92) was a false positive.** The
agent filled one of 42 gaps and reported 41 remaining — correct arithmetic
across *two* payloads, `gap_count` from `detect_shift_gaps` and the new shift
from `schedule_shift`. `_grounded_number_set` derives differences only within a
single record, so cross-payload arithmetic is unverifiable to it. Fourth
instance of the same family as the stray `7.0` and the ISO-8601 dates: a true
statement the scorer cannot see. **Not yet fixed.**

`generates_donor_report` passed while saying "a 42-day coverage gap" — it is 42
blocks across 7 days. Grounding scored 16/16 because `42` was in the payload.
Same blind spot as the helicopters: the number is real, the frame is wrong.

### 2. The judge could not see the operation it was judging — `6a9db66`

`score_judge` received the command, the tool payload and the answer, but never
the briefing the agent was given. So the operation's own name read as an
invention — *"it introduces the name 'Cyclone Vega'"* — and the rubric's own
rule ("any claim that is not supported must score 2 or below") forced a 2.

**This marked down 8 of 14 cases in run 6**, including a flawless "450 of 500,
short 50" that scored grounding 3. It is the stray `7.0` one layer up: a true
statement that lives in the prompt rather than in a payload. Same treatment —
the briefing is named once in `describe_disaster()` and read from there by both
the agent's message and the judge's context, so they cannot drift apart.

### 3. Absence is not a measurement — `6a9db66`

Two rules added to the orchestrator contract. The first forbids converting an
empty result into a count. The second carves "empty" out of "insufficient":
the prompt had been telling the agent to *"call them again with different
arguments if the first result is insufficient"*, which is the instruction it
followed four times into an empty table.

### 4. Loop health measured volume, not redundancy — `6a9db66`

An assessment consulting six distinct sources and a lookup asking one source
five times both read as "too many rounds" and drew the identical complaint.
Across 29 recorded case runs, correct answers call each tool exactly once and
only the flailing case exceeds two, so `max_tool_repeats` separates them
cleanly. The pre-existing duplicate check does not: it counts only calls the
orchestrator *refused* as identical, which an agent slips past by varying its
arguments.

### 5. Run 7 — 3/3, every dimension 1.00, 17,358 tokens

| case | v6 | v7 |
|---|---|---|
| `does_not_fabricate_missing_data` | FAIL 0.71 | **PASS 1.00** |
| `simple_lookup_is_efficient` | 0.97 (judge 0.83) | **1.00 (judge 1.00)** |
| `does_not_invent_weather_risk` | 0.95 (judge 0.75) | **1.00 (judge 1.00)** |

The fabrication case now answers "no records of helicopters or boats deployed
are tracked for this event" and states no numbers at all. `search_notes` calls
went 4 → 2, iterations 8 → 3, tokens 16,867 → 7,059.

The weather case is a controlled comparison: its answer still says "Cyclone
Vega, Coastal Zone", the exact phrase condemned in v6, and the only thing that
changed is what the judge was shown. Grounding 2 → 5.

### 6. Two changes made today were never exercised

`situation_assessment_multi_tool` scored 1.00 in v6 but called four tools in
five iterations, inside the *old* budget of 6 — the raise to 7 did nothing.
`schedules_shift` wrote no note, so `allowed_extra_writes` never triggered.
Both remain unverified against a live model, and a pass on a case whose scoring
just changed is the weakest evidence available.

## 2026-09-03

No code changed. One targeted eval run, `eval_report_v5.json` — the five cases
the budget cut off yesterday. **5/5 passed, mean 0.99, 56,185 tokens.**

Together with v4 this completes the post-fix picture: all 12 cases have now been
assessed against `8821499`, and 11 of them pass.

### All four constants are confirmed against a live model

Yesterday two of the four were confirmed live and two by tests only. Both gaps
are closed — though not by the cases that were predicted to close them.

| constant | confirmed by | evidence |
|---|---|---|
| `30.0` critical threshold | v4 `no_false_shortage_alarm` | "below the 30 % critical threshold", 8/8 traced |
| `7.0` gap-scan window | v4 `schedules_shift…`, v5 `generates_donor_report` | "a gap of 42 persons for the next week", 14/14 traced |
| `5.0` severity scale | **v5 `situation_assessment_multi_tool`** | "Cyclone Vega remains active with severity 4/5", 22/22 traced |
| `8.0` round limit | **v5 `does_not_fabricate_missing_data`** | ran the full 8 iterations, so the notice fired; grounding still 1.00 |

**`severity_scale_max` was confirmed by the wrong case, which is worth
recording.** The prediction was that `simple_lookup_is_efficient` would turn
FAIL 0.88 → PASS because its `[5.0]` came from "severity 2/5". It passed at
0.98 — but its answer never mentions severity this run. It called only
`get_supply_status` and talked about sandbags. The `[5.0]` did not recur because
the sentence that carried it was never written, exactly the way the stray `7.0`
disappeared from `situation_assessment_multi_tool` in v3 without being fixed.

The real confirmation came from `situation_assessment_multi_tool`, which stated
"severity 4/5" and had all 22 of its numeric claims traced. That case also
called `get_weather_forecast` **and** `detect_shift_gaps` in one run — the first
time the coverage tool has been exercised by the case that originally flagged
the `7.0`, and it grounded cleanly ("42 four-hour gaps", from `gap_count` and
`block_hours`).

The `8.0` confirmation is unambiguous: `does_not_fabricate_missing_data` ran
exactly `MAX_TOOL_ITERATIONS` = 8 iterations, so `hit_iteration_limit` fired and
the round-limit sentence was emitted. It landed in `run_notices`, and grounding
scored 4/4 with no stray `8.0`. Before the fix that same sentence cost the case
twice — once on the scorer, once on the judge.

### Loop health is now the only dimension below 1.00

| dimension | v5 mean |
|---|---|
| tool_selection | 1.00 |
| grounding | 1.00 |
| side_effects | 1.00 |
| judge | 0.98 |
| **loop_health** | **0.90** |

Two cases exceed their iteration budget, and both are real agent behaviour
rather than scoring artefacts:

- `does_not_fabricate_missing_data` — 8 iterations against a budget of 4,
  `search_notes` ×5 over data that does not exist. Predicted yesterday, and it
  held. The case still passes overall because `loop_health` carries a weight of
  0.10 (`evals/dataset.py:54`), so 0.75 on that dimension costs 0.025.
- `situation_assessment_multi_tool` — 7 against a budget of 6, which is the cost
  of it calling six distinct tools including the weather forecast.

That the case can pass at 0.975 while flailing through eight rounds is the
honest reading of the aggregate: **loop health is being measured but is barely
being priced.**

### Budget

56,185 tokens for five cases, against the ~40k estimated in the handoff — 40%
over. The estimate was extrapolated from v3's per-case average, which
undercounts the multi-tool cases: `situation_assessment_multi_tool` alone cost
22,640 and `does_not_fabricate_missing_data` 16,625, together 70% of the run.

Groq's headers expose only the per-minute bucket (`x-ratelimit-limit-tokens:
8000`), never the daily one, so **the rolling-window question from yesterday
cannot be settled without hitting a 429.** Not worth a deliberate wall.

## 2026-09-02

One commit, `8821499`. 16 files, +290 / −29. Tests 98 → 110.

The eval suite ran for the first time since the ISO-date fix and since the tool
count went 20 → 21. Both changes came back clean. The run also settled an open
question about the grounding scorer, and the answer was not the one predicted.

---

### 1. What the eval runs measured

`eval_report_v3.json` — 12/12 assessable, **9/12 (75%)**, mean 0.96, 96,260
tokens. Against the true full-suite baseline of 7/12 (58%), and against the
aborted 9-case run that scored 78%, the comparable first nine cases went 8/9
(89%).

- **The ISO-8601 fix is confirmed end to end.** `identifies_overdue_tasks`
  passed with 6/6 claims traced; its old `[8.0, 30.0, 8.0, 30.0]` signature is
  gone, and no date-shaped false positive appears anywhere in the 12 cases.
- **The 20 → 21 tool growth cost nothing in selection.** `tool_selection`
  scored 1.00 on all 12 cases. `get_weather_forecast` was even chosen unprompted
  during a general situation assessment, which answers the open question of
  whether its docstring was discoverable.

### 2. Constants the agent quotes are now returned, not hidden — `8821499`

`situation_assessment_multi_tool` was predicted to keep failing on a stray
`7.0` from "no shift coverage for the next 7 days". It passed — but not because
anything was fixed. It simply never called `detect_shift_gaps` that run, so
there was no window to describe. The defect was intact, and it surfaced in three
*other* cases wearing different clothes:

```
no_false_shortage_alarm        UNGROUNDED [30.0]  <- "threshold <30 % availability"
simple_lookup_is_efficient     UNGROUNDED [5.0]   <- "severity 2/5"
does_not_fabricate_missing_data UNGROUNDED [8.0]  <- "the 8-round tool limit"
situation_assessment_multi_tool UNGROUNDED [7.0]  <- "the next 7 days"   (run 2)
```

Every one is a **true statement about the system itself** — a scan window, a
criticality threshold, a severity scale — that the agent read from a docstring
and that no tool ever put in its payload. Narrowing what counts as a claim would
have caught one of the four; loosening the scorer would have solved the problem
by making it blind, which is the one thing a grounding check cannot afford.

The tools now return what they reasoned over:

| tool | added |
|---|---|
| `detect_shift_gaps` | `window_days`, `block_hours`, `min_volunteers`, `gap_count` |
| `get_supply_status` | `critical_threshold_pct` on each record |
| `get_disaster_status` | `severity_scale_max` |

The values are named once in `database/crud.py` and `database/models.py`, then
read from there by the queries, the tool payloads, the Pydantic bounds and the
agent prompts, so the figure an agent states and the figure a query used cannot
drift apart.

**`detect_shift_gaps`'s tool payload is now an object, not a bare list** — a
deliberate breaking change to that contract. Wrapping it is what lets the window
survive an empty result, which is exactly the case where an agent says "no gaps
over the next 7 days" and has nothing to cite. `crud`'s own return shape is
unchanged, so `report_agent` and `shift_agent` were untouched.

### 3. The orchestrator's own footer is no longer scored as an agent claim

`agents/orchestrator.py` appended *"Orchestrator stopped at the 8-round tool
limit"* into `alerts`. Alerts are the agent's claims about the operation; that
footer is the harness describing its own execution. Mixing them cost twice: the
grounding scorer flagged the `8`, and the LLM judge independently returned
grounding=2 citing that sentence as the answer's *only* unsupported claim.

It moves to a new `run_notices` field, rendered on the dashboard as a muted
notice rather than an alert.

### 4. Verification, and why it is not the eval run

`eval_report_v4.json` confirmed both fixes against a live model before the daily
budget ran out at case 8:

- `no_false_shortage_alarm` **FAIL → PASS**, 8/8 traced, stating "below the 30 %
  critical threshold" — the exact sentence that failed.
- `schedules_shift_from_available_volunteers` stated "Remaining gaps across the
  7-day window" and scored grounding **14/14, 1.00** — the exact `7.0` failure,
  now grounded.

But no LLM-driven run can be *relied* on to exercise this, because tool choice
belongs to the model: `detect_shift_gaps` sits in `any_of_tools`, so a case can
satisfy its constraint without ever calling it. That is precisely how the bug
sidestepped one run and reappeared the next.

So the binding verification is deterministic and costs nothing:
`tests/test_grounded_constants_e2e.py` drives the real tool through the real
orchestrator with a scripted model and scores it with the real scorer. Every
accepted constant is paired with a test that rejects the wrong value, so this
widened what the scorer can see without widening what it excuses.

---

## 2026-09-01

Seven commits, `c8e2661..9eaac26`. 19 files, +4,382 / −1,904. Tests 55 → 98.

Two themes: the evaluation harness was failing correct answers, and the
dashboard was a single 2,161-line file with no responsive behaviour. A weather
tool, a landing page, and API protection followed.

---

### 1. Two grounding scorer bugs — `c8e2661`

The harness scored 7/12 (58%). Four of the five failures were the harness's
fault, not the agent's.

**Recommendations scored as factual claims.** `_answer_text` fed
`next_recommended_actions` to `score_grounding`. Those are forward-looking
proposals by the orchestrator's own contract, so they carry planning horizons
and suggested quantities that cannot trace to tool output:

```
UNGROUNDED [24.0]       <- "Monitor consumption over the next 24-48 hours"
UNGROUNDED [10.0, 15.0] <- "Pre-position an additional 10-15 units"
```

Grounding is a hard pass gate, so one such phrase flipped a 0.97 case to FAIL.
Grounding now scores `_claim_text` — summary plus alerts. The LLM judge still
receives the recommendations in full.

**ISO-8601 hyphens read as minus signs.** The regex `-?\d+(?:\.\d+)?` parsed
`"2026-08-30T08:00:00"` as `2026, -8, -30`, so date components entered the
grounded set with the wrong sign. When the agent correctly restated a due date
in prose it produced `+8, +30`, matched nothing, and **every due date the agent
echoed back was reported as a hallucination.** Fixed with a negative lookbehind;
genuine negatives still parse as negative.

Result: 7/9 assessable, 78%, mean 0.96.

**A correction worth recording:** I first reported a third bug — that the supply
status colours contradicted themselves. They do not. `is_critical` is derived at
`crud.py:416` using `ratio < 0.3`, the same rule as the frontend's `pct < 30`.
Generators and Emergency Tents sit at exactly 30.0%, the boundary. Correct
boundary behaviour, misread as inconsistency.

Four regression tests added.

---

### 2. Frontend split, responsive, contrast — `8aaf9e6`

`static/index.html` was 2,161 lines with the entire stylesheet and application
inline. This commit reduced it to 295 lines of markup plus `static/css/app.css`
and `static/js/app.js`; later phases in this changelog grew the markup to 342.
The split was done first and verified before anything else changed.

**Responsive** — the stylesheet previously had **zero** `@media` rules, a hard
`width: 220px` sidebar and `overflow: hidden` on `<body>`, so narrow viewports
could not reach the content:

| | |
|---|---|
| > 1024px | full sidebar, unchanged desktop layout |
| 769–1024px | sidebar collapses to a 72px icon rail |
| ≤ 768px | off-canvas drawer behind a menu button; app-shell lock released |

The drawer closes on backdrop click, Escape, choosing a destination, and leaving
the breakpoint.

**Contrast** — `--text-muted` measured 2.88:1 against `--card` and `--text-dim`
1.75:1, both under the 4.5:1 AA floor, on text carrying the actual data. Now
5.42:1 and 3.74:1.

**Type scale** — the old scale ran 18px against 14px body, a 1.29:1 range that
reads as having no hierarchy. Section titles now `clamp(26px, 3vw, 36px)`
against 11px letterspaced labels.

**Supply cards** — severity moved from a full-width banner to a left edge. The
banner added height to critical cards only, so titles and bars sat at different
heights across a row. One `supplySeverity()` helper now feeds the bar, edge and
badge so they cannot disagree. The badge replaces a pill that read "needed" on
all eight cards; category pills went neutral so colour means severity only.

Also: `?v=` on assets, and `prefers-reduced-motion` honoured.

---

### 3. Command Center overview — `8defac5`

The view opened to ~60% empty viewport. Two panels now carry it, both from
existing endpoints.

**Operation overview** — a KPI row of stat tiles, not a chart: these are
headline counts and the number is the visualisation. One hero figure plus four
tiles. Every value from `GET /disasters/{id}`, which already returned
`critical_supplies`, `volunteers_assigned`, `tasks_open`, `shifts_today`,
`supplies_total` — one request.

**Status colour stays out of the digits.** The hero value wears the `--text`
token; a separate dot and sentence ("3 items below 30% of need") carries state.
A red number reads as "this number is wrong" rather than "this situation is
bad", and colour alone is not accessible.

**Needs attention** — the critical supplies and open tasks behind those counts,
worst first, with explicit empty states.

Supply meters: unfilled track is now a light step of the fill's own hue.

Fixed while testing: `auto-fit` wrapped tiles 3+1 leaving a hole; the floating
demo button covered the last rows; the overview needed to stack below 768px.

---

### 4. Landing page and motion — `90f1339`

`/` serves `static/landing.html`; the dashboard moved to `/dashboard`.

The reference requested was a marketing site; this is an ops tool, so what was
borrowed is the **typography, not the genre**. A display serif runs to 82px
against 11px letterspaced mono — roughly 7:1, against the dashboard's original
1.29:1. The colour field was deliberately not borrowed: red means *critical* in
this product, so a full-bleed red landing page would spend a signal the
dashboard needs.

Every figure was verified before being written — 20 tools (confirmed against
`get_tool_names()` at runtime), 5 agents, 6 languages, 12 eval cases, MIT. Hero
artwork is inline SVG, so the page has no asset dependency.

**Motion**, two jobs only: say a fetch is in flight, and make a section change
read as a change of view. Supply-grid skeletons in the shape of the cards
(replacing a spinner that said nothing and let the layout jump); KPI values dim
during refetch; sections fade and rise; landing sections reveal on first
intersection with a stagger. The hidden state is applied by script, so with
scripting off the page shows everything rather than nothing. All suppressed
under `prefers-reduced-motion`.

---

### 5. Weather forecast tool — `17611c0`

The previous twenty tools all read and wrote the same SQLite file. This is the
first that reaches outside it.

`get_weather_forecast(disaster_id)` returns rainfall, peak wind, when rain
starts, and the next six hours. Open-Meteo: no key, no registration.

**ReliefWeb was the better source and is gated.** v1 returns 410
("decommissioned, use v2"); v2 requires an appname approved on request.

**Locations resolve through a ladder,** because the geocoder matches bare place
names only. `"Metro Manila, Philippines"` returns nothing, and so does
`"Metro Manila"` — without prefix stripping to `"Manila"`, the only disaster in
the demo database would have had no forecast at all. The ladder tries the full
string, the first comma segment, that segment minus a leading administrative
word, then the trailing segment flagged approximate.

**Degradation**, since this is the one tool that can fail for outside reasons:

- Never raises. Failures return `{"available": false, reason, guidance}`, with
  availability stated explicitly rather than the key being absent, so a model
  cannot read a failed lookup as a calm forecast.
- Geocodes cache 24h, forecasts 15min — the dashboard polls every 30s, which
  uncached is ~2,880 requests/day against a free service.
- A failed refresh with an expired entry serves the stale reading, flagged.
- 6s timeout: the tool runs inside the agent loop.

Evals pin `RELIEFAI_WEATHER_MODE=offline`. **The seeded corpus is untouched.**

Eleven tests, all offline. One exists because wiring the eval default leaked
`RELIEFAI_WEATHER_MODE` into the pytest process, silently short-circuiting three
degradation tests into the offline fixture.

---

### 6. Auth split and cache headers — `094cea2`

**The API key protected nothing.** `render.yaml` set `API_KEY` to
`reliefai-demo-key` in production and the dashboard printed that value into the
page. Confirmed against the live site: `GET /disasters` with the key copied from
page source returned 200.

Endpoints now split by cost, not verb:

| | Keyed |
|---|---|
| Reads that only touch the database | no |
| Every write | yes |
| Everything spending model tokens — `/command`, `/assess`, `/gaps`, `GET /report` | yes |

That last row is why "all GETs public" would have been wrong:
`GET /disasters/{id}/report` runs the report agent.

The demo key stays public deliberately, so a visitor can press *Deploy Agents*.
This closes casual writes, not determined ones, and the README now says so.

Missing auth also answered **422** because the required `Header(...)` made
validation reject before the check ran. Now **401**.

**Redeploys did not reach returning visitors.** `StaticFiles` served
`index.html` with no cache directives, so a browser that had seen the old
dashboard kept it — the live site had new markup while a returning browser
showed the old layout. `?v=` could not help, because the HTML naming those files
was the stale part. Middleware now sets `no-cache, must-revalidate` on HTML,
one year immutable on `?v=` assets, five minutes otherwise. A conditional GET on
unchanged HTML returns 304.

---

### 7. Rate limiting — `9eaac26`

Two layers, because they stop different things.

**Per-IP throttling** — 120 reads/min, 60 writes/hour, 10 model calls/hour.
Stops one client hammering the API; a bot with a hundred addresses walks past it.

**A daily token budget** — the layer that matters. The scarce resource is Groq's
200,000 tokens/day; when they are gone every agent call fails for everyone.

**The cap is denominated in tokens, not requests.** Measured runs range 8,140 to
18,774 tokens, so twelve might spend 98k or 225k. A first attempt capped calls
at 200/day, which would have permitted **3.75M tokens against a 200k budget —
19× the ceiling it was meant to defend.** Spend is now reported from the Groq
client, so every call counts: orchestrator loop, specialists and eval judge. A
small call cap remains as a secondary guard, because spend is only known after a
call returns.

Default 150,000/day, leaving room for an eval run. Visible on `/health`. When it
trips, **reads keep working** — the dashboard stays browsable.

**Client identity is the last `X-Forwarded-For` entry, not the first.** The
proxy appends the address it observed; reading the first would let a caller pick
their own identity and mint unlimited buckets. There is a test for that attack.
The tracking table is bounded with LRU eviction.

Counters are in memory — Render's free tier is one instance that sleeps, so
there is nowhere shared to put them. They reset on restart; a second replica
would need Redis.

---

## Not done

- **The eval suite has not been run since the ISO-date fix**, and the tool count
  has since gone 20 → 21. The next run measures both at once.
- No eval case exercises `get_weather_forecast`.
- The model has not been observed *choosing* the weather tool unprompted; if it
  does not, the fix is likely its docstring.
- ReliefWeb appname not requested.
