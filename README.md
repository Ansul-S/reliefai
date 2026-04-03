# ReliefAI — Post-Disaster Relief Orchestrator

ReliefAI is a multi-agent AI system that coordinates disaster relief operations in real time. It solves the fragmented coordination problem where volunteer schedules, supply tracking, and community communications are scattered across WhatsApp groups and spreadsheets. A single natural-language command dispatches a network of specialist AI agents — volunteer scheduler, supply chain manager, multilingual communications agent, shift coverage detector, and report generator — all coordinated by a central orchestrator running on Groq's Llama 3.3 70B. Built with FastAPI, async SQLite, and FastMCP tool servers, deployed on Google Cloud Run for a total infrastructure cost of $0.

---

## Architecture

```
User / Dashboard
      │
      ▼
┌─────────────────────────────┐
│     Orchestrator Agent      │  ← Groq Llama 3.3 70B
│   (Primary Coordinator)     │
└────────────┬────────────────┘
             │ delegates to
   ┌─────────┼──────────┬──────────────┬──────────────┐
   ▼         ▼          ▼              ▼              ▼
Volunteer  Supply    Comms Agent   Shift Agent   Report Agent
  Agent    Agent    (Translations) (Gap Detect)  (Reports)
   └─────────┴──────────┴──────────────┴──────────────┘
                         │
              ┌──────────▼──────────────┐
              │     MCP Tool Server     │
              │  Calendar | Tasks |     │
              │  Notes | Supplies |     │
              │  Broadcasts             │
              └──────────┬──────────────┘
                         │
                ┌────────▼────────┐
                │   SQLite DB     │
                │  (8 tables)     │
                └─────────────────┘
```

---

## Tech Stack

| Component   | Technology               | Cost  |
|-------------|--------------------------|-------|
| AI Agents   | Groq API (Llama 3.3 70B) | Free  |
| Backend     | FastAPI + Python 3.11    | Free  |
| Database    | SQLite + SQLAlchemy      | Free  |
| MCP Tools   | FastMCP                  | Free  |
| Deployment  | Google Cloud Run         | Free  |
| Repository  | GitHub                   | Free  |
| **Total**   |                          | **$0**|

---

## Prerequisites

- Python 3.11+
- Free Groq API key from [console.groq.com](https://console.groq.com)
- Docker (for Cloud Run deployment only)
- Google Cloud account (for Cloud Run deployment only)

---

## Local Development

### 1. Clone the repository

```bash
git clone YOUR_GITHUB_REPO_URL
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

### 5. Seed demo data

Open a **new terminal** (keep the server running):

```bash
source venv/bin/activate
python seed_demo.py
```

This creates the full Typhoon Carina scenario and runs 5 live AI commands.

### 6. Test the main endpoint

```bash
curl -X POST http://localhost:8000/disasters/1/command \
  -H "X-API-Key: reliefai-demo-key" \
  -H "Content-Type: application/json" \
  -d '{"command": "Assess current situation and identify critical issues"}'
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

## Demo Walkthrough (5 commands)

Run these in order after seeding. All use `POST /disasters/1/command` with `X-API-Key: reliefai-demo-key`.

### Command 1 — Situation Assessment

```json
{"command": "Assess current situation and identify top 3 critical issues"}
```

**Expected:** Orchestrator calls `supply_agent` + `shift_agent`, flags water/baby food critical shortage and 3 cancelled shifts.

### Command 2 — Shift Gap Recovery

```json
{"command": "3 volunteers cancelled their shifts today. Find replacements immediately."}
```

**Expected:** `shift_agent` detects 3 open slots, `volunteer_agent` matches available volunteers by skill and schedules replacements.

### Command 3 — Multilingual Broadcast

```json
{"command": "Draft emergency broadcast in Filipino and English about Rizal High School evacuation center"}
```

**Expected:** `comms_agent` translates into 6 languages (EN/HI/TA/ID/TL/JA), enforces SMS character limits, saves broadcast.

### Command 4 — Donor Report

```json
{"command": "Generate a donor status report"}
```

**Expected:** `report_agent` aggregates all DB data and generates an impact-focused Markdown report.

### Command 5 — Procurement Task

```json
{"command": "Create procurement task for emergency water — 500L available, need 2000L"}
```

**Expected:** `supply_agent` flags critical shortage, creates procurement task with high priority in the DB.

---

## Project Structure

```
reliefai/
│
├── agents/                    # AI agent layer
│   ├── __init__.py
│   ├── _groq_client.py        # Shared Groq API helper
│   ├── orchestrator.py        # Primary coordinator
│   ├── volunteer_agent.py     # Scheduling & assignments
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
├── mcp_server/                # MCP tool server
│   ├── __init__.py
│   └── tools.py               # FastMCP tool definitions
│
├── schemas/                   # Pydantic v2 schemas
│   ├── __init__.py
│   └── models.py              # Request + response models
│
├── static/                    # Frontend
│   └── index.html             # Single-file ops dashboard
│
├── main.py                    # FastAPI application
├── seed_demo.py               # Demo data seeder
├── run_local.sh               # One-command local setup
├── Dockerfile                 # Container definition
├── requirements.txt           # Python dependencies
├── .env.example               # Environment template
├── .gitignore
└── .dockerignore
```

---

## GitHub Setup

```bash
git init
git add .
git commit -m "Initial commit — ReliefAI Post-Disaster Relief Orchestrator"
git branch -M main
git remote add origin YOUR_GITHUB_REPO_URL
git push -u origin main
```

---

## Cloud Run Deployment

### Step 1 — Authenticate and configure

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com
```

### Step 2 — Create artifact repository

```bash
gcloud artifacts repositories create reliefai \
  --repository-format=docker \
  --location=asia-southeast1
```

### Step 3 — Build and push Docker image

```bash
docker build -t asia-southeast1-docker.pkg.dev/YOUR_PROJECT_ID/reliefai/app:latest .
docker push asia-southeast1-docker.pkg.dev/YOUR_PROJECT_ID/reliefai/app:latest
```

### Step 4 — Deploy to Cloud Run

```bash
gcloud run deploy reliefai \
  --image asia-southeast1-docker.pkg.dev/YOUR_PROJECT_ID/reliefai/app:latest \
  --platform managed \
  --region asia-southeast1 \
  --allow-unauthenticated \
  --set-env-vars GROQ_API_KEY=your_groq_key_here,API_KEY=reliefai-demo-key \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300
```

### Step 5 — Seed demo data after deploy

Run this once after every deployment:

```bash
curl -X POST https://YOUR-CLOUD-RUN-URL.run.app/demo/seed \
  -H "X-API-Key: reliefai-demo-key"
```

### Step 6 — Verify deployment

```bash
curl https://YOUR-CLOUD-RUN-URL.run.app/health
```

> **Important:** SQLite database resets on every redeploy because it lives inside the container. Always run the seed command after deploying. For a production upgrade, migrate to **Google Cloud Firestore** (no schema management, scales automatically, persistent across deploys).

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GROQ_API_KEY` | Your Groq API key (required) | — |
| `API_KEY` | Dashboard authentication key | `reliefai-demo-key` |
| `DATABASE_URL` | SQLAlchemy async DB URL | `sqlite+aiosqlite:///./relief.db` |

---

## License

MIT
