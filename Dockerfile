FROM python:3.11-slim

# Hugging Face Spaces runs containers as a non-root user (uid 1000).
RUN useradd -m -u 1000 user

WORKDIR /app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

# Writable location for the SQLite file (ephemeral — the app auto-seeds on boot)
RUN mkdir -p /app/data && chown -R user:user /app

USER user

ENV HOME=/home/user \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    DATABASE_URL=sqlite+aiosqlite:////app/data/relief.db

EXPOSE 7860

# Shell form so $PORT expands (HF Spaces uses 7860, Cloud Run injects 8080)
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}
