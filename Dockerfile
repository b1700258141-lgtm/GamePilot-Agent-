FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels gamepilot \
    && useradd --uid 10001 --create-home gamepilot \
    && mkdir /app/artifacts \
    && chown gamepilot:gamepilot /app/artifacts
COPY alembic.ini ./
COPY alembic/ ./alembic/

USER gamepilot
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import json, urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)) == {'status': 'ready'}"]
CMD ["python", "-m", "uvicorn", "gamepilot.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
