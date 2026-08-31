#!/bin/bash
set -e

echo "=================================="
echo "  ReliefAI — Local Setup"
echo "=================================="

# ── Create virtual environment ─────────────────────────────────────
if [ ! -d "venv" ]; then
  python -m venv venv
  echo "✓ Virtual environment created."
else
  echo "✓ Virtual environment already exists."
fi

# ── Activate ───────────────────────────────────────────────────────
source venv/bin/activate
echo "✓ Virtual environment activated."

# ── Install dependencies ───────────────────────────────────────────
echo ""
echo "Installing dependencies..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo "✓ Dependencies installed."

# ── Environment file ───────────────────────────────────────────────
if [ ! -f .env ]; then
  # Written inline rather than copied from a template, so setup works on a
  # fresh clone with no .env.example present.
  cat > .env <<'ENVFILE'
GROQ_API_KEY=your_groq_key_here
API_KEY=reliefai-demo-key
GROQ_MODEL=openai/gpt-oss-120b
MAX_TOOL_ITERATIONS=8
DATABASE_URL=sqlite+aiosqlite:///./relief.db
ENVFILE
  echo ""
  echo "================================================"
  echo "  ACTION REQUIRED: Add your Groq API key"
  echo "================================================"
  echo ""
  echo "  1. Open the .env file in your editor:"
  echo "     nano .env   (or code .env, vim .env, etc.)"
  echo ""
  echo "  2. Replace 'your_groq_key_here' with your key"
  echo ""
  echo "  3. Get a FREE key at: https://console.groq.com"
  echo "     (No credit card needed — takes 30 seconds)"
  echo ""
  echo "  The .env file looks like this:"
  echo "     GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxx"
  echo "     API_KEY=reliefai-demo-key"
  echo "     DATABASE_URL=sqlite+aiosqlite:///./relief.db"
  echo ""
  echo "Press Enter once you have added GROQ_API_KEY to .env..."
  read -r
else
  echo "✓ .env file already exists."
fi

# ── Verify Groq key is set ─────────────────────────────────────────
GROQ_KEY=$(grep "GROQ_API_KEY" .env | cut -d= -f2 | tr -d '[:space:]')
if [ "$GROQ_KEY" = "your_groq_key_here" ] || [ -z "$GROQ_KEY" ]; then
  echo ""
  echo "⚠  WARNING: GROQ_API_KEY not set in .env"
  echo "   The server will start but AI agents will fail."
  echo "   Get your free key at: https://console.groq.com"
  echo ""
fi

# ── Static directory ───────────────────────────────────────────────
mkdir -p static

# ── Start server ───────────────────────────────────────────────────
echo ""
echo "================================================"
echo "  Starting ReliefAI server..."
echo "================================================"
echo ""
echo "  Dashboard:  http://localhost:8000"
echo "  API Docs:   http://localhost:8000/docs"
echo "  Health:     http://localhost:8000/health"
echo ""
echo "  Demo data seeds itself on first run — nothing to do."
echo ""
echo "  In a new terminal:"
echo "    source venv/bin/activate"
echo "    pytest                    # 55 tests, no API key needed"
echo "    python -m evals --list    # evaluation cases"
echo "    python seed_demo.py       # optional: 5 live AI commands"
echo ""
echo "  Press Ctrl+C to stop the server."
echo "================================================"
echo ""

uvicorn main:app --reload --port 8000