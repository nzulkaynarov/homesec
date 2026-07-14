#!/usr/bin/env bash
# Pull-деплой: тянет main из GitHub и, если появились изменения, обновляет
# зависимости и перезапускает панель. Запускается системным таймером раз в
# минуту (см. homesec-update.timer). Конфиг — в /etc/default/homesec-deploy.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/default/homesec-deploy   # DEPLOY_USER, APP_DIR, BRANCH

run() { sudo -u "$DEPLOY_USER" "$@"; }

run git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
local_sha=$(run git -C "$APP_DIR" rev-parse HEAD)
remote_sha=$(run git -C "$APP_DIR" rev-parse "origin/$BRANCH")

if [ "$local_sha" = "$remote_sha" ]; then
  exit 0
fi

echo "$(date -Is) homesec update: $local_sha -> $remote_sha"
run git -C "$APP_DIR" reset --hard "origin/$BRANCH"
run "$APP_DIR/backend/.venv/bin/pip" install -q -r "$APP_DIR/backend/requirements.txt"
systemctl restart homesec
# Бот — отдельный юнит. Прод ставился до его появления, поэтому деплой
# устанавливает юнит сам (update.sh выполняется от root из systemd-таймера).
if ! systemctl cat homesec-bot >/dev/null 2>&1; then
  sed "s/^User=.*/User=$DEPLOY_USER/" "$APP_DIR/deploy/homesec-bot.service" \
    > /etc/systemd/system/homesec-bot.service
  systemctl daemon-reload
  systemctl enable homesec-bot
fi
systemctl restart homesec-bot
echo "$(date -Is) homesec restarted"
