FROM python:3.11-slim

# Run as a non-root user, as container platforms expect.
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

# Shell form so $PORT expands — Render injects its own port at runtime
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}
