# ReliefAI — Post-Disaster Relief Orchestrator

**Live demo → https://reliefai-zmsv.onrender.com**

> Hosted on Render's free plan, which sleeps after 15 minutes of inactivity.
> The first request wakes it and takes roughly 50 seconds; everything after
> that is fast. Demo data seeds itself on boot, so the dashboard is always
> populated.

ReliefAI is a multi-agent AI system that coordinates disaster relief operations in real time. It solves the fragmented coordination problem where volunteer schedules, supply tracking, and community communications are scattered across WhatsApp groups and spreadsheets. A single natural-language command goes to an orchestrator agent that selects and calls tools from an MCP tool server through the LLM's native function-calling API — reading live operational data, reasoning over the results, and calling further tools until it can answer. Built with FastAPI, async SQLite, and a FastMCP tool server, running on Groq and deployed on Render for a total infrastructure cost of $0.

---

## Architecture

ReliefAI is a **tool-using agent**, not a fixed pipeline. The orchestrator does
not decide in advance which specialists to run: it receives the command, is
given the full tool catalogue, and drives its own loop of tool calls until it
has enough information to answer.

```
User / Dashboard
      │
      ▼
POST /disasters/{id}/command
      │
      ▼
┌───────────────────────────────┐
│      Orchestrator Agent       │  ← Groq (gpt-oss-120b)
│                               │
│   ┌───────────────────────┐   │
│   │ 1. LLM reasoning      │   │
│   │ 2. tool selection     │◀──┼──── tool schemas from MCP
│   └───────────┬───────────┘   │
└───────────────┼───────────────┘
                │ native function calling
                │ (tool_calls)
                ▼
     ┌──────────────────────┐
     │   Tool registry      │   validates name + arguments,
     │  agents/tool_registry│   caps result size, converts
     │                      │   failures into tool errors
     └──────────┬───────────┘
                │ mcp.call_tool(name, args)
                ▼
     ┌──────────────────────┐
     │   MCP Tool Server    │   20 tools — FastMCP
     │  mcp_server/tools.py │   situation | volunteers | shifts
     │                      │   supplies | tasks | notes | comms
     └──────────┬───────────┘
                │
       ┌────────▼────────┐
       │   SQLite DB     │
       │   (8 tables)    │
       └────────┬────────┘
                │ tool results
                ▼
        back to the LLM ──► more tools if needed (max 8 rounds)
                                    │
                                    ▼
                         structured final response
                            (JSON mode, no tools)
                                    │
                                    ▼
                                API / UI
```

The four stages are distinct:

| Stage | Where it happens |
|-------|------------------|
| **LLM reasoning** | Groq chat completion, tool schemas attached |
| **Tool selection** | The model emits `tool_calls`; nothing in the codebase routes by keyword |
| **Tool execution** | `agents/tool_registry.py` → `mcp.call_tool()` → `mcp_server/tools.py` |
| **Final response** | A last call with `tools=[]` and JSON mode, so the model must answer |

Tool calls requested in the same turn run concurrently. The loop is bounded by
`MAX_TOOL_ITERATIONS` (default 8), and a byte-identical repeated call is refused
after the second attempt.

### Specialist agents

Two agents own genuine LLM domain logic and are exposed to the orchestrator *as
tools*, so there is only ever one implementation of each capability:

| Agent | Reachable as | Also serves |
|-------|--------------|-------------|
| `comms_agent` | `translate_and_send_broadcast` tool | `POST /disasters/{id}/broadcasts` |
| `report_agent` | `generate_situation_report` tool | `GET /disasters/{id}/report` |
| `supply_agent` | — | `POST /disasters/{id}/supplies/assess` |
| `shift_agent` | — | `POST /disasters/{id}/shifts/gaps` |

---

## Tech Stack

| Component   | Technology               | Cost  |
|-------------|--------------------------|-------|
| Agent loop  | Groq native tool calling | Free  |
| Backend     | FastAPI + Python 3.11    | Free  |
| Database    | SQLite + SQLAlchemy      | Free  |
| Tools       | FastMCP (20 tools)       | Free  |
| Tests       | pytest + pytest-asyncio  | Free  |
| Evals       | Custom harness, 5 dims   | Free  |
| Deployment  | Render (Docker)          | Free  |
| Repository  | GitHub                   | Free  |
| **Total**   |                          | **$0**|

---

## Prerequisites

- Python 3.11+
- Free Groq API key from [console.groq.com](https://console.groq.com)
- Docker (for container builds only)
- A Render account (for deployment only)

---

## Local Development

### 1. Clone the repository

```bash
git clone https://github.com/Ansul-S/reliefai.git
cd reliefai
```

### 2. Get your free Groq API key

1. Go to [https://console.groq.com](https://console.groq.com)
2. Sign up (no credit card needed — takes 30 seconds)
3. Navigate to **API Keys** → **Create API Key**
4. Copy the key (starts with `gsk_`)

### 3. Start the server

```bash
bash run_local.sh
```

The script will:
- Create a virtual environment
- Install all dependencies
- Prompt you to add your `GROQ_API_KEY` to `.env`
- Start the server at `http://localhost:8000`

### 4. Open in browser

| URL | Description |
|-----|-------------|
| http://localhost:8000 | Operations Dashboard |
| http://localhost:8000/docs | Interactive API Explorer |
| http://localhost:8000/health | Health Check |

### 5. Demo data

The Typhoon Carina scenario **seeds itself on startup** whenever the database is
empty, so there is nothing to run. To additionally fire five live AI commands
through the orchestrator, open a second terminal:

```bash
source venv/bin/activate
python seed_demo.py
```

### 6. Test the main endpoint

```bash
curl -X POST http://localhost:8000/disasters/1/command \
  -H "X-API-Key: reliefai-demo-key" \
  -H "Content-Type: application/json" \
  -d '{"command": "Assess current situation and identify critical issues"}'
```

### 7. Run the checks

```bash
pytest                  # 55 tests, no API key or network needed
python -m evals --list  # see the evaluation cases
```

---

## API Endpoints

| Method  | Endpoint                            | Description                          |
|---------|-------------------------------------|--------------------------------------|
| `POST`  | `/disasters`                        | Create disaster event                |
| `GET`   | `/disasters`                        | List all disasters                   |
| `GET`   | `/disasters/{id}`                   | Get disaster details + counts        |
| `POST`  | `/disasters/{id}/command`           | **Main orchestrator endpoint** ⭐     |
| `POST`  | `/volunteers`                       | Register volunteer                   |
| `GET`   | `/disasters/{id}/volunteers`        | List volunteers + today's hours      |
| `POST`  | `/disasters/{id}/volunteers/{v}/assign` | Assign volunteer to disaster     |
| `GET`   | `/disasters/{id}/supplies`          | Supply status with critical flags    |
| `POST`  | `/disasters/{id}/supplies`          | Add supply item                      |
| `PATCH` | `/supplies/{id}`                    | Update supply quantities             |
| `POST`  | `/disasters/{id}/supplies/assess`   | Run supply agent assessment          |
| `GET`   | `/disasters/{id}/shifts`            | Get shifts (optional `?date=`)       |
| `POST`  | `/disasters/{id}/shifts`            | Create shift                         |
| `POST`  | `/disasters/{id}/shifts/gaps`       | Detect gaps + AI auto-fill           |
| `GET`   | `/disasters/{id}/broadcasts`        | List broadcasts                      |
| `POST`  | `/disasters/{id}/broadcasts`        | Translate + save broadcast           |
| `GET`   | `/disasters/{id}/report`            | Generate report (`?type=donor\|authority\|internal`) |
| `GET`   | `/disasters/{id}/tasks`             | List tasks (optional `?status=`)     |
| `POST`  | `/disasters/{id}/tasks`             | Create task                          |
| `PATCH` | `/tasks/{id}`                       | Update task status                   |
| `GET`   | `/disasters/{id}/logs`              | Recent agent activity logs           |
| `POST`  | `/demo/seed`                        | Seed Typhoon Carina scenario         |
| `POST`  | `/demo/simulate`                    | Run all 5 demo commands              |
| `GET`   | `/health`                           | Health check                         |

---

### Orchestrator response shape

`POST /disasters/{id}/command` returns the fields the dashboard has always read,
plus tracing added alongside the tool-calling loop:

```jsonc
{
  "orchestrator_summary": "…",          // the agent's answer
  "agents_called":  ["get_supply_status", "create_task"],  // tools, kept under
  "tools_called":   ["get_supply_status", "create_task"],  // the old key name
  "results":        { "get_supply_status": [...] },        // raw tool output
  "alerts":                   ["Water critically low"],
  "next_recommended_actions": ["Order 1600L of water"],
  "execution_time_ms": 51234,

  // Added with the tool-calling loop
  "iterations": 3,
  "model": "openai/gpt-oss-120b",
  "trace": [
    { "step": "llm",  "iteration": 1, "duration_ms": 847, "finish_reason": "tool_calls" },
    { "step": "tool", "iteration": 1, "duration_ms": 10, "tool": "get_supply_status",
      "arguments": { "disaster_id": 1 }, "ok": true }
  ],
  "usage": {
    "input_tokens": 7442, "output_tokens": 784, "total_tokens": 8226,
    "llm_ms": 94121, "tool_ms": 22, "estimated_cost_usd": 0.0017043
  }
}
```

`agents_called` is retained under its original name for dashboard
compatibility, but it now lists the tools the model chose rather than a fixed
roster of prompt agents. Unknown keys are ignored by the frontend, so the added
fields are backward compatible.

---

## Demo Walkthrough (5 commands)

Run these in order after seeding. All use `POST /disasters/1/command` with `X-API-Key: reliefai-demo-key`.

### Command 1 — Situation Assessment

```json
{"command": "Assess current situation and identify top 3 critical issues"}
```

**Expected:** The model calls `get_disaster_status` and `get_supply_status`, then flags the water and baby-formula shortages from the real numbers. Exact tool choice varies between runs — that is the point.

### Command 2 — Shift Gap Recovery

```json
{"command": "3 volunteers cancelled their shifts today. Find replacements immediately."}
```

**Expected:** `detect_shift_gaps` finds the open slots, `find_available_volunteers` supplies candidates, and `schedule_shift` books replacements.

### Command 3 — Multilingual Broadcast

```json
{"command": "Draft emergency broadcast in Filipino and English about Rizal High School evacuation center"}
```

**Expected:** `translate_and_send_broadcast` renders 6 languages (EN/HI/TA/ID/TL/JA), enforces SMS limits, and saves the broadcast.

### Command 4 — Donor Report

```json
{"command": "Generate a donor status report"}
```

**Expected:** `generate_situation_report` aggregates the DB and returns an impact-focused Markdown report.

### Command 5 — Procurement Task

```json
{"command": "Create procurement task for emergency water — 500L available, need 2000L"}
```

**Expected:** `get_supply_status` confirms the shortfall, then `create_task` writes a high-priority procurement task to the DB.

---

## Tool Inventory

All 20 tools are defined once, in `mcp_server/tools.py`, and registered on a
FastMCP server. The orchestrator reaches them through `mcp.call_tool()` — the
same path an external MCP client would use — so there is no second copy of any
tool implementation.

| Tool | Arguments | Purpose |
|------|-----------|---------|
| `get_disaster_status` | `disaster_id` | Situation overview + counts |
| `get_supply_status` | `disaster_id` | Supplies with `is_critical` flags |
| `update_supply` | `supply_id`, `quantity_available`, `status` | Adjust stock |
| `get_volunteers` | `disaster_id` | Roster with skills and hours today |
| `find_available_volunteers` | `disaster_id`, `required_skills?` | Skill-matched candidates |
| `get_shifts` | `disaster_id`, `date_filter?` | Scheduled shifts |
| `schedule_shift` | `disaster_id`, `volunteer_id`, `role`, `start_time`, `end_time` | Book a shift |
| `cancel_shift` | `shift_id`, `reason?` | Cancel with reason |
| `detect_shift_gaps` | `disaster_id` | Under-covered 4-hour blocks |
| `create_task` | `disaster_id`, `title`, `description`, … | Create a task |
| `update_task` | `task_id`, `status`, `notes?` | Advance a task |
| `list_tasks` | `disaster_id`, `status_filter?` | List tasks |
| `get_overdue_tasks` | `disaster_id` | Past-due, incomplete tasks |
| `add_note` | `disaster_id`, `content`, `tags?`, `created_by?` | Record intel |
| `search_notes` | `disaster_id`, `query` | Keyword search |
| `draft_broadcast` | `disaster_id`, `message`, `target_audience`, `channel?` | Draft a message |
| `send_broadcast` | `broadcast_id` | Mark as sent |
| `get_broadcasts` | `disaster_id` | Broadcast history |
| `translate_and_send_broadcast` | `disaster_id`, `message`, `languages?`, `channel?` | Multilingual broadcast (wraps `comms_agent`) |
| `generate_situation_report` | `disaster_id`, `report_type?` | Written report (wraps `report_agent`) |

The FastMCP server also runs standalone for use by any MCP client:

```bash
python -m mcp_server.tools
```

---

## Observability

Every orchestration run writes a trace to the `agent_logs` table and returns it
in the API response, so a command can be read as a timeline:

```
User command
  ├─ LLM       847ms   finish=tool_calls   tokens=1546
  ├─ TOOL       10ms   get_supply_status({"disaster_id": 1})
  ├─ LLM     30190ms   finish=tool_calls   tokens=2339
  ├─ TOOL       12ms   create_task({...})
  └─ LLM     19180ms   finish=stop         tokens=2497
```

The response `usage` block carries `input_tokens`, `output_tokens`,
`total_tokens`, `llm_ms`, `tool_ms` and `estimated_cost_usd`.

---

## Testing

```bash
source venv/bin/activate
pytest
```

The suite runs against a throwaway SQLite database and a scripted fake LLM, so
it needs no API key and makes no network calls. Tool execution is real: calls go
through the MCP registry to the test database.

Covered: tool discovery and schema generation, valid and invalid dispatch,
unknown tools, malformed arguments, tool exceptions, oversized results, the
multi-step tool loop, concurrent tool calls in one turn, duplicate-call
suppression, the iteration ceiling, malformed final JSON, and the API contract.

---

## Evaluations

Tests prove the tool loop *runs*; evals measure whether the agent's tool choices
and answers are actually *good*. That is the property which degrades silently
when a model is swapped, a prompt is edited, or a tool description is reworded —
a failure mode this project has already been bitten by once, when Groq retired
the previous model.

```bash
python -m evals                 # full suite
python -m evals --list          # show the cases
python -m evals --no-judge      # deterministic scorers only (free, fast)
python -m evals --tag grounding --json report.json
```

Exit status is non-zero if any case fails, so CI can gate on it.

### How a case works

Each case seeds a controlled scenario into a clean database, runs the real
orchestrator against it, and scores the outcome. Ground truth is read back out
of the database at run time rather than hardcoded, so a scenario and its
expectations cannot drift apart.

### The five dimensions

| Dimension | Kind | What it catches |
|-----------|------|-----------------|
| `tool_selection` | deterministic | Missing the right tool, or reaching for a forbidden one |
| `grounding` | deterministic | **Hallucinated numbers** — every figure stated must trace to tool output |
| `side_effects` | deterministic | Writes on read-only commands; missing writes on action commands |
| `loop_health` | deterministic | Failed tool calls, duplicate calls, runaway iteration counts |
| `judge` | LLM | Relevance and operational usefulness, which the others cannot see |

Four of the five cost nothing and never flake. `--no-judge` runs only those.

The grounding scorer accepts arithmetic the agent legitimately performs on real
data — stating a 1600-liter shortfall when the tools returned 2000 needed and
400 available is grounded, not invented — but rejects any figure it cannot
derive from what the tools actually returned.

### What the dataset covers

Beyond the happy path, the cases deliberately include:

- **No-false-alarm**: every supply is well stocked; an agent that pattern-matches
  "disaster" to "shortage" invents a crisis and fails.
- **Fabrication probe**: asks about helicopters and boats, which the database
  does not track at all. The correct answer is to say so.
- **Read-only discipline**: a plain count question must not write to any table.
- **Precision**: a roster where one volunteer is inactive and two lack the
  requested skill, so a sloppy answer names the wrong people.

The scorers themselves are unit-tested (`tests/test_eval_scorers.py`) against
known-bad agent behaviour. An eval harness that cannot fail is worse than none —
it manufactures confidence.

### Budgeting a run

Groq's free tier allows **8,000 tokens per minute** and **200,000 per day**. A
single case costs roughly 5k-12k tokens, so a full 12-case run is around 150k —
most of a day's budget, and enough to keep the suite in rate-limit backoff
throughout. In practice:

```bash
python -m evals --no-judge --delay 20     # ~25% cheaper, paced to the limiter
python -m evals --tag grounding           # run one slice at a time
python -m evals --limit 4                 # smoke test
```

Runs are reported honestly against these limits. A case that fails because the
provider rate-limited it is marked `[ERR ]`, excluded from the pass rate, and
never counted as a quality regression — and the suite stops early rather than
manufacturing a page of identical failures once the daily budget is gone.

Exit codes: `0` all assessed cases passed, `1` a case genuinely failed, `3`
nothing failed but some cases could not be assessed.

---

## Project Structure

```
reliefai/
│
├── agents/                    # AI agent layer
│   ├── __init__.py
│   ├── _groq_client.py        # Shared Groq API helper
│   ├── orchestrator.py        # Tool-calling agent loop
│   ├── tool_registry.py       # MCP → LLM schema/exec adaptor
│   ├── supply_agent.py        # Inventory & procurement
│   ├── comms_agent.py         # Multilingual broadcasts
│   ├── shift_agent.py         # Coverage gap detection
│   └── report_agent.py        # Report generation
│
├── database/                  # Data layer
│   ├── __init__.py            # Async engine + get_db
│   ├── models.py              # SQLAlchemy ORM (8 tables)
│   └── crud.py                # All async CRUD functions
│
├── mcp_server/                # MCP tool server — single source of truth
│   ├── __init__.py
│   └── tools.py               # 20 FastMCP tool definitions
│
├── schemas/                   # Pydantic v2 schemas
│   ├── __init__.py
│   └── models.py              # Request + response models
│
├── static/                    # Frontend
│   └── index.html             # Single-file ops dashboard
│
├── evals/                     # Evaluation harness
│   ├── scenarios.py           # Deterministic seeded worlds
│   ├── dataset.py             # Golden cases + expectations
│   ├── scorers.py             # 4 deterministic + 1 LLM judge
│   └── runner.py              # Execution and reporting
│
├── tests/                     # pytest suite
│   ├── conftest.py            # Test DB + scripted fake LLM
│   ├── test_tool_registry.py  # Schemas, dispatch, failure modes
│   ├── test_orchestrator_loop.py  # The tool-calling loop
│   └── test_eval_scorers.py   # The scorers reject bad behaviour
│
├── main.py                    # FastAPI application
├── seed_demo.py               # Optional: runs 5 live AI commands
├── run_local.sh               # One-command local setup
├── pytest.ini                 # Test configuration
├── render.yaml                # Render blueprint (deployment)
├── Dockerfile                 # Container definition
├── requirements.txt           # Python dependencies
├── .gitignore
└── .dockerignore
```

> There is no `.env.example` in the repo. `run_local.sh` writes a starter `.env`
> for you on first run; otherwise create one from the **Environment Variables**
> table below. `.env` is gitignored and must never be committed.

---

## Development Workflow

The repository is [github.com/Ansul-S/reliefai](https://github.com/Ansul-S/reliefai).
Render watches `main` and redeploys automatically on push, so shipping is:

```bash
pytest                       # must pass — no network needed
git push origin main         # Render builds and deploys from render.yaml
curl https://reliefai-zmsv.onrender.com/health
```

Before changing a prompt, a tool description, or `GROQ_MODEL`, run the evals —
those edits change agent behaviour in ways the unit tests cannot see:

```bash
python -m evals --no-judge --delay 20
```

---

## Deployment (Render)

The repo ships a `render.yaml` blueprint, so deployment is fully declarative.

### Step 1 — Create the service

1. Sign in at [render.com](https://render.com) with GitHub (free tier needs no card)
2. **New +** → **Blueprint**
3. Select the `reliefai` repository — Render reads `render.yaml` automatically

### Step 2 — Set the secret

Render prompts for `GROQ_API_KEY` (marked `sync: false`, so it is never committed).
Paste your key from [console.groq.com](https://console.groq.com). The other
variables come from the blueprint.

### Step 3 — Verify

```bash
curl https://reliefai-zmsv.onrender.com/health
```

```json
{"status":"ok","db":"connected","ai_provider":"groq",
 "model":"openai/gpt-oss-120b","version":"1.0.0"}
```

> Render hostnames are globally unique. This service is `reliefai-zmsv` because
> the bare `reliefai` name was already taken by an unrelated project — dropping
> the suffix sends people somewhere else entirely.

Demo data seeds itself on startup — no manual seed step required.

> **Note:** the SQLite database lives inside the container and resets on every
> restart, which is why the app re-seeds the Typhoon Carina scenario on boot.
> On Render's free plan the service also sleeps after 15 minutes of inactivity;
> the first request afterwards takes roughly 50 seconds to wake it. For a
> production upgrade, move to Render Postgres or a managed document store.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GROQ_API_KEY` | Your Groq API key (**required**) | — |
| `API_KEY` | Dashboard / API authentication key | `reliefai-demo-key` |
| `GROQ_MODEL` | Groq model id — **must support native tool calling** | `openai/gpt-oss-120b` |
| `MAX_TOOL_ITERATIONS` | Hard cap on tool-calling rounds per command | `8` |
| `DATABASE_URL` | SQLAlchemy async DB URL | `sqlite+aiosqlite:///./relief.db` |
| `RELIEFAI_EVAL_DB` | Where the eval harness puts its throwaway database | system temp dir |

On Render, `API_KEY`, `GROQ_MODEL` and `MAX_TOOL_ITERATIONS` come from
`render.yaml`; only `GROQ_API_KEY` is entered by hand, because it is marked
`sync: false` and so is never committed.

### If the agents suddenly stop working

Groq retires model ids without much notice, and the failure is quiet from the
outside: the server boots, `/health` returns `ok`, and every agent call errors.
Check the id is still served before assuming a code problem:

```bash
curl -H "Authorization: Bearer $GROQ_API_KEY" \
     https://api.groq.com/openai/v1/models
```

Any replacement must support both native tool calling and JSON mode. Swap it via
the `GROQ_MODEL` environment variable — no code change or image rebuild needed.

---

## License

MIT
