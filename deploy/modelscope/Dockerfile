FROM python:3.12-slim AS builder
WORKDIR /build
COPY modelscope-source.tar.gz ./
RUN tar -xzf modelscope-source.tar.gz && python -m pip wheel --no-cache-dir --wheel-dir /wheels ".[agent]"

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GAMEPILOT_PUBLIC_PREVIEW=true
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels "gamepilot[agent]" \
    && python -c "from pathlib import Path; import gamepilot; p = Path(gamepilot.__file__).parent / 'web' / 'index.html'; assert 'agent-skill-content' in p.read_text(encoding='utf-8')" \
    && useradd --uid 10001 --create-home gamepilot \
    && mkdir -p /app/artifacts/agent-skills \
    && chown -R gamepilot:gamepilot /app/artifacts
COPY --from=builder /build/alembic.ini ./
COPY --from=builder /build/alembic/ ./alembic/
USER gamepilot
EXPOSE 7860
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import json, urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:7860/ready', timeout=2)) == {'status': 'ready'}"]
CMD ["python", "-m", "uvicorn", "gamepilot.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
