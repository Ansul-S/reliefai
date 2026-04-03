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
  cp .env.example .env
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
echo "  The server auto-seeds demo data on first run."
echo ""
echo "  After server starts, run in a new terminal:"
echo "    source venv/bin/activate"
echo "    python seed_demo.py"
echo ""
echo "  Press Ctrl+C to stop the server."
echo "================================================"
echo ""

uvicorn main:app --reload --port 8000