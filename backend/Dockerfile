# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/models
WORKDIR /srv

# CPU-only torch (the default PyPI wheel drags in ~2 GB of CUDA libraries we never use).
# The version is taken from the pinned requirements so the two can never drift apart.
COPY backend/requirements.txt ./requirements.txt
RUN pip install "$(grep -i '^torch==' requirements.txt)" --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

COPY backend ./backend
RUN mkdir -p /srv/backend/storage && useradd -m app && chown -R app /srv /models 2>/dev/null || true
USER app
WORKDIR /srv/backend
EXPOSE 8000
# Apply migrations, then serve. The checkpointer tables are created by the app on startup.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
