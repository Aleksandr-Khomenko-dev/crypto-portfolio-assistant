# One image for every role (runner, internal API, public webhook app, migrations);
# docker-compose.yml picks the command. Secrets come from .env at runtime only.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Editable install: the code stays in /app, so PROJECT_ROOT-relative paths
# (config/, app/static/, .cache/) resolve exactly as on the Mac.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install -e ".[telegram-news]"

COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY config ./config

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /data /app/logs /app/.cache \
    && chown -R bot:bot /data /app/logs /app/.cache
USER bot

# SQLite lives on the /data volume (overridden in docker-compose.yml).
ENV CPDA_DATABASE_URL=sqlite:////data/dev.db

CMD ["python", "-m", "app.scanner.runner"]
