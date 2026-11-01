#!/usr/bin/env bash
# Установка моста «схема -> RCON» на Ubuntu/Debian.
# Запускать из папки с проектом (где лежат server.py, index.html).
#   sudo bash deploy/install.sh
set -euo pipefail

APP_DIR="/opt/railway-switches"
SERVICE="railway-switches"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Запусти с sudo: sudo bash deploy/install.sh"
  exit 1
fi

echo "==> Проверяю Python 3"
if ! command -v python3 >/dev/null 2>&1; then
  echo "==> Ставлю python3"
  apt-get update
  apt-get install -y python3
fi
python3 -V

echo "==> Копирую файлы в $APP_DIR"
mkdir -p "$APP_DIR"
for f in server.py index.html commands.json switches.json; do
  cp "$SRC_DIR/$f" "$APP_DIR/$f"
done
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$SRC_DIR/.env.example" "$APP_DIR/.env"
  echo "    создан $APP_DIR/.env (пока без пароля — режим заглушки)"
fi
chown -R www-data:www-data "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Служба systemd"
sed "s#/opt/railway-switches#$APP_DIR#g" "$SRC_DIR/deploy/$SERVICE.service" > "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

echo
echo "Готово. Дальше:"
echo "  1) nano $APP_DIR/.env      # RCON_HOST / RCON_PORT / RCON_PASSWORD, API_KEY"
echo "  2) systemctl restart $SERVICE"
echo "  3) curl http://127.0.0.1:3000/api/ping"
echo "  4) journalctl -u $SERVICE -f"
