FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY bot ./bot
RUN pip install --no-cache-dir . && useradd -r -u 10001 botuser && mkdir -p /app/bot/data /app/bot/reports \
    && chown -R botuser /app/bot/data /app/bot/reports
USER botuser
# El modo se pasa por BOT_MODE (paper por defecto). `live` requiere TTY para la confirmación en consola.
ENV BOT_MODE=paper
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import json,sys,datetime as d;h=json.load(open('/app/bot/data/health.json'));t=d.datetime.fromisoformat(h['ts']);sys.exit(0 if (d.datetime.now(d.UTC)-t).total_seconds()<180 else 1)"
CMD ["sh", "-c", "python -m bot.main --mode ${BOT_MODE}"]
