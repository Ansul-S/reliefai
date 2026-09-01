# Changelog

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
