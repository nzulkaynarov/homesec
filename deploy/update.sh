#!/usr/bin/env bash
# Pull-деплой: тянет main из GitHub и, если появились изменения, обновляет
# зависимости и перезапускает панель. Запускается системным таймером раз в
# минуту (см. homesec-update.timer). Конфиг — в /etc/default/homesec-deploy.
#
# Ключевое свойство: «выкачено» = дошли до конца, а не «HEAD совпал с origin».
# Раньше сравнивался HEAD, а reset --hard шёл ДО pip install: упавший pip
# (нет сети, зависимость не собралась на Pi 3) оставлял на диске новый код,
# HEAD == origin/main, и все следующие тики выходили как «изменений нет» —
# деплой заклинивало навсегда, а при ближайшем рестарте панель уходила в
# crash-loop без установленной зависимости. Теперь метка пишется последней:
# любой сбой на любом шаге просто ретраится через минуту.
set -euo pipefail

# shellcheck disable=SC1091
source /etc/default/homesec-deploy   # DEPLOY_USER, APP_DIR, BRANCH

STATE_DIR=/var/lib/homesec
DEPLOYED_FILE="$STATE_DIR/deployed_sha"

run() { sudo -u "$DEPLOY_USER" "$@"; }

# Юниты синхронизируются ДО раннего выхода «изменений нет»: иначе новый или
# изменённый юнит из свежего коммита никогда не установится — деплой, привёзший
# его, исполняет ещё старый update.sh (bash держит прежний inode), а все
# следующие тики выходят раньше. Ловили вживую с homesec-bot.
# Пишем только при реальном отличии, чтобы не дёргать daemon-reload каждую минуту.
sync_units() {
  local changed=0 name rendered installed
  for name in homesec.service homesec-bot.service; do
    [ -f "$APP_DIR/deploy/$name" ] || continue
    rendered=$(sed "s/^User=.*/User=$DEPLOY_USER/" "$APP_DIR/deploy/$name")
    installed=$(cat "/etc/systemd/system/$name" 2>/dev/null || true)
    if [ "$rendered" != "$installed" ]; then
      printf '%s\n' "$rendered" > "/etc/systemd/system/$name"
      echo "$(date -Is) homesec update: юнит $name обновлён"
      changed=1
    fi
  done
  for name in homesec-update.service homesec-update.timer \
              homesec-dnscheck.service homesec-dnscheck.timer; do
    [ -f "$APP_DIR/deploy/$name" ] || continue
    if ! cmp -s "$APP_DIR/deploy/$name" "/etc/systemd/system/$name"; then
      cp "$APP_DIR/deploy/$name" "/etc/systemd/system/$name"
      echo "$(date -Is) homesec update: юнит $name обновлён"
      changed=1
    fi
  done
  if [ "$changed" = 1 ]; then
    systemctl daemon-reload
    systemctl enable --now homesec homesec-bot homesec-update.timer \
      homesec-dnscheck.timer >/dev/null 2>&1 || true
  fi
}

run git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
remote_sha=$(run git -C "$APP_DIR" rev-parse "origin/$BRANCH")
deployed_sha=$(cat "$DEPLOYED_FILE" 2>/dev/null || true)

sync_units

if [ "$deployed_sha" = "$remote_sha" ]; then
  exit 0
fi

echo "$(date -Is) homesec update: ${deployed_sha:-нет метки} -> $remote_sha"

# Зависимости ставим ДО reset --hard: если pip упадёт, на диске остаётся
# рабочий старый код (иначе Jinja2 с auto_reload уже читал бы новые шаблоны
# под старым процессом).
tmp_req=$(mktemp)
trap 'rm -f "$tmp_req"' EXIT
run git -C "$APP_DIR" show "origin/$BRANCH:backend/requirements.txt" > "$tmp_req"
chmod 0644 "$tmp_req"   # mktemp даёт 0600 root — пользователю деплоя не прочесть
run "$APP_DIR/backend/.venv/bin/pip" install -q -r "$tmp_req"

run git -C "$APP_DIR" reset --hard "origin/$BRANCH"
sync_units              # второй раз: применяем юниты уже из нового коммита
systemctl restart homesec
systemctl restart homesec-bot

# Метка успеха — последней строкой. Всё, что выше, при сбое ретраится.
mkdir -p "$STATE_DIR"
echo "$remote_sha" > "$DEPLOYED_FILE"
echo "$(date -Is) homesec restarted, deployed $remote_sha"
