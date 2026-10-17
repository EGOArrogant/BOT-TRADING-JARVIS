#!/usr/bin/env bash
# JARVIS en un servidor en la nube (Ubuntu 22.04 o 24.04): corre 24/7 aunque tu PC esté apagada.
#
# En el servidor, copia y pega esta línea (te irá preguntando lo necesario):
#   curl -fsSL https://raw.githubusercontent.com/EGOArrogant/BOT-TRADING-JARVIS/main/deploy/instalar_servidor.sh | sudo bash
#
# Qué hace:
#   1. Instala Python 3.12 (con uv), descarga JARVIS en /opt/jarvis y sus piezas.
#   2. Pide tus llaves (Anthropic; Telegram opcional) y las guarda en /opt/jarvis/.env (solo lectura para el bot).
#   3. Revisa que Binance responda desde este servidor (si responde 451/403, este país está bloqueado).
#   4. Crea los servicios jarvis-bot y jarvis-panel: arrancan solos y se reinician si fallan o tras actualizar.
#   5. Instala Tailscale (red privada gratis): el panel solo se puede abrir desde tus dispositivos de Tailscale
#      y además pide la clave del QR. Nunca queda abierto a todo internet.
set -euo pipefail

REPO="https://github.com/EGOArrogant/BOT-TRADING-JARVIS.git"
DIR="/opt/jarvis"
USR="jarvis"
PORT=8765
TTY=/dev/tty

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ask() { local v; read -r -p "$1" v < "$TTY"; printf '%s' "$v"; }

[ "$(id -u)" -eq 0 ] || { echo "Ejecútalo con sudo."; exit 1; }

say "1/6 · Paquetes del sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates qrencode iptables >/dev/null

# Servidores pequeños (1 GB de RAM): 2 GB de memoria de intercambio para que nada se quede sin memoria
if [ "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" -lt 1800 ] && ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

say "2/6 · Descargando JARVIS en $DIR"
id "$USR" >/dev/null 2>&1 || useradd -r -m -d "/home/$USR" -s /bin/bash "$USR"
if [ -d "$DIR/.git" ]; then
  sudo -u "$USR" git -C "$DIR" pull --ff-only
else
  git clone -q "$REPO" "$DIR"
  chown -R "$USR:$USR" "$DIR"
fi
sudo -u "$USR" bash -c 'command -v ~/.local/bin/uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null'
UV="/home/$USR/.local/bin/uv"
[ -x "$DIR/.venv/bin/python" ] || sudo -u "$USR" "$UV" venv -q --python 3.12 "$DIR/.venv"
sudo -u "$USR" "$UV" pip install -q --python "$DIR/.venv/bin/python" -e "$DIR"

say "3/6 · Tus llaves (se guardan solo en este servidor)"
ENVF="$DIR/.env"
[ -f "$ENVF" ] || cp "$DIR/.env.example" "$ENVF"
set_env() {  # set_env CLAVE valor
  if grep -q "^$1=" "$ENVF"; then sed -i "s|^$1=.*|$1=$2|" "$ENVF"; else echo "$1=$2" >> "$ENVF"; fi
}
if ! grep -q '^ANTHROPIC_API_KEY=..*' "$ENVF"; then
  k=$(ask "Pega tu llave de Anthropic (empieza con sk-ant-) y presiona Enter: ")
  [ -n "$k" ] && set_env ANTHROPIC_API_KEY "$k"
fi
if ! grep -q '^TELEGRAM_BOT_TOKEN=..*' "$ENVF"; then
  t=$(ask "Token de tu bot de Telegram (Enter para omitir): ")
  if [ -n "$t" ]; then
    c=$(ask "Tu chat_id de Telegram (lo da configurar_telegram en la PC o @userinfobot): ")
    set_env TELEGRAM_BOT_TOKEN "$t"; set_env TELEGRAM_CHAT_ID "$c"
  fi
fi
chown "$USR:$USR" "$ENVF" && chmod 600 "$ENVF"
touch "$DIR/celular_activado.txt" && chown "$USR:$USR" "$DIR/celular_activado.txt"

say "4/6 · Revisando que Binance responda desde este servidor"
if ! sudo -u "$USR" bash -c "cd $DIR && .venv/bin/python -m bot.revisar" < "$TTY"; then
  echo "La revisión encontró un problema (arriba dice cuál). Si Binance está bloqueado aquí, este servidor no sirve:"
  echo "crea uno en otra región (por ejemplo México) y vuelve a correr este instalador."
  exit 1
fi

say "5/6 · Servicios que arrancan solos"
for svc in bot panel; do
  if [ "$svc" = bot ]; then CMD="-m bot.main --mode paper"; DESC="JARVIS: bot (demo)"; else
    CMD="-m bot.main panel --mode paper --celular --port $PORT"; DESC="JARVIS: panel"; fi
  cat > "/etc/systemd/system/jarvis-$svc.service" <<EOF
[Unit]
Description=$DESC
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=$USR
WorkingDirectory=$DIR
Environment=PATH=/home/$USR/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/.venv/bin/python $CMD
# Sale con 10 tras el botón Actualizar del panel: systemd lo vuelve a arrancar con el código nuevo
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
done
systemctl daemon-reload
systemctl enable --now jarvis-bot jarvis-panel >/dev/null

say "6/6 · Tailscale: acceso privado desde tu iPhone en cualquier red"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
# El panel solo acepta conexiones que llegan por Tailscale (y luego pide la clave del QR)
iptables -C INPUT -i tailscale0 -j ACCEPT 2>/dev/null || iptables -I INPUT -i tailscale0 -j ACCEPT
iptables -C INPUT -p tcp --dport "$PORT" ! -i tailscale0 -j DROP 2>/dev/null || iptables -I INPUT 2 -p tcp --dport "$PORT" ! -i tailscale0 -j DROP
if command -v ip6tables >/dev/null; then
  ip6tables -C INPUT -p tcp --dport "$PORT" ! -i tailscale0 -j DROP 2>/dev/null || ip6tables -I INPUT -p tcp --dport "$PORT" ! -i tailscale0 -j DROP
fi
apt-get install -y -qq iptables-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true
echo "Se abrirá un enlace: ábrelo en tu PC o iPhone e inicia sesión en Tailscale (gratis) para autorizar este servidor."
tailscale up --hostname=jarvis < "$TTY" || true

KEYF="$DIR/bot/reports/paper/panel_clave.txt"
for _ in $(seq 30); do [ -s "$KEYF" ] && break; sleep 1; done
TSIP=$(tailscale ip -4 2>/dev/null | head -1 || true)
say "Listo: JARVIS corre 24/7 en este servidor."
if [ -n "$TSIP" ] && [ -s "$KEYF" ]; then
  URL="http://$TSIP:$PORT/?t=$(cat "$KEYF")"
  echo "En tu iPhone: instala la app Tailscale, entra con la MISMA cuenta y enciéndela."
  echo "Luego escanea este código con la cámara (o abre la dirección en Safari):"
  qrencode -t ansiutf8 "$URL" || true
  echo "$URL"
  echo "En Safari: Compartir → Agregar a pantalla de inicio, y JARVIS queda como una app."
else
  echo "Termina de iniciar sesión en Tailscale y vuelve a correr este instalador para ver el código QR."
fi
echo
echo "Comandos útiles:  sudo systemctl status jarvis-bot   ·   sudo journalctl -u jarvis-bot -f"
