#!/usr/bin/env bash
# Установка панели HomeSec на Raspberry Pi (Raspberry Pi OS / Debian).
#
# На малинке:
#   sudo git clone https://github.com/nzulkaynarov/homesec.git /opt/homesec
#   sudo bash /opt/homesec/deploy/install.sh
#
# Ставит venv, systemd-сервис панели и таймер авто-обновления из GitHub.
set -euo pipefail

APP_DIR=/opt/homesec
BRANCH=main
DEPLOY_USER="${SUDO_USER:-$(whoami)}"

echo "==> Пользователь деплоя: $DEPLOY_USER, каталог: $APP_DIR"
chown -R "$DEPLOY_USER":"$DEPLOY_USER" "$APP_DIR"
sudo -u "$DEPLOY_USER" git config --global --add safe.directory "$APP_DIR" || true

echo "==> Python и venv"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip
sudo -u "$DEPLOY_USER" python3 -m venv "$APP_DIR/backend/.venv"
sudo -u "$DEPLOY_USER" "$APP_DIR/backend/.venv/bin/pip" install -q --upgrade pip
sudo -u "$DEPLOY_USER" "$APP_DIR/backend/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"

if [ ! -f "$APP_DIR/backend/.env" ]; then
  sudo -u "$DEPLOY_USER" cp "$APP_DIR/backend/.env.example" "$APP_DIR/backend/.env"
  echo "!!! Заполните $APP_DIR/backend/.env (пароли MikroTik, AdGuard, админа), затем: sudo systemctl restart homesec"
fi

echo "==> systemd: сервис панели"
sed "s/^User=.*/User=$DEPLOY_USER/" "$APP_DIR/deploy/homesec.service" > /etc/systemd/system/homesec.service

echo "==> systemd: таймер авто-обновления из GitHub"
cat > /etc/default/homesec-deploy <<EOF
DEPLOY_USER=$DEPLOY_USER
APP_DIR=$APP_DIR
BRANCH=$BRANCH
EOF
cp "$APP_DIR/deploy/homesec-update.service" /etc/systemd/system/homesec-update.service
cp "$APP_DIR/deploy/homesec-update.timer" /etc/systemd/system/homesec-update.timer

systemctl daemon-reload
systemctl enable --now homesec
systemctl enable --now homesec-update.timer

echo "==> Готово."
echo "    Панель:  http://$(hostname -I | awk '{print $1}'):8000"
echo "    Логи:    journalctl -u homesec -f"
echo "    Деплой:  journalctl -u homesec-update -f"
