#!/usr/bin/env sh
# Uso en cron cada 5 min: alerta si el latido (health.json) tiene más de 3 minutos.
F="${1:-/opt/crypto-bot/bot/data/health.json}"
python3 - "$F" <<'PY'
import json, sys, datetime as d
h = json.load(open(sys.argv[1]))
age = (d.datetime.now(d.UTC) - d.datetime.fromisoformat(h["ts"])).total_seconds()
print(f"latido hace {age:.0f}s · fuentes caídas: {h.get('unhealthy')} · modo seguro: {h.get('safe_mode')}")
sys.exit(0 if age < 180 else 1)
PY
