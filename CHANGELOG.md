# Changelog

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
