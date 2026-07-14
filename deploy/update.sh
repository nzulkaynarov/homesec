#!/usr/bin/env bash
# Pull-деплой: тянет main из GitHub и, если появились изменения, обновляет
# зависимости и перезапускает панель. Запускается системным таймером раз в
# минуту (см. homesec-update.timer). Конфиг — в /etc/default/homesec-deploy.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/default/homesec-deploy   # DEPLOY_USER, APP_DIR, BRANCH

run() { sudo -u "$DEPLOY_USER" "$@"; }

# Юниты доустанавливаются ДО раннего выхода «изменений нет»: иначе новый
# юнит из свежего коммита никогда не установится — деплой, привёзший его,
# исполняет ещё старый update.sh (bash держит прежний inode), а все
# следующие тики выходят раньше. Ловили вживую с homesec-bot.
ensure_units() {
  if ! systemctl cat homesec-bot >/dev/null 2>&1; then
    sed "s/^User=.*/User=$DEPLOY_USER/" "$APP_DIR/deploy/homesec-bot.service" \
      > /etc/systemd/system/homesec-bot.service
    systemctl daemon-reload
    systemctl enable --now homesec-bot
  fi
}

run git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
local_sha=$(run git -C "$APP_DIR" rev-parse HEAD)
remote_sha=$(run git -C "$APP_DIR" rev-parse "origin/$BRANCH")

if [ "$local_sha" = "$remote_sha" ]; then
  ensure_units
  exit 0
fi

echo "$(date -Is) homesec update: $local_sha -> $remote_sha"
run git -C "$APP_DIR" reset --hard "origin/$BRANCH"
run "$APP_DIR/backend/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"
systemctl restart homesec
ensure_units
systemctl restart homesec-bot
echo "$(date -Is) homesec restarted"
