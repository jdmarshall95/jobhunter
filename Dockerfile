# syntax=docker/dockerfile:1
# jobhunter — односложный образ: Python 3.12 + зависимости + код.
FROM python:3.12-slim

# tzdata нужен APScheduler/zoneinfo для TIMEZONE=Europe/Moscow.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer is reused when only the code changes.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        $(python -c "import tomllib; print(' '.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))")

# Application code, configs and prompts (see app/config.py APP_ROOT).
COPY app ./app
COPY config ./config
COPY prompts ./prompts

# Non-root: the data/ volume must stay writable for the app user.
RUN useradd --create-home --uid 1000 appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# No curl in the slim image: plain urllib against the built-in /healthz server.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app.main"]
