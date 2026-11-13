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

REJECTED_FILE="$STATE_DIR/rejected_sha"
HEALTH_URL=http://127.0.0.1:8000/api/luna/v1/health

run() { sudo -u "$DEPLOY_USER" "$@"; }

# Сообщение родителям через бота HomeSec. Токен читается из .env и не попадает
# ни в аргументы процессов, ни в журнал.
notify() {
  local env_file="$APP_DIR/backend/.env" token ids id
  token=$(sed -n 's/^HS_TELEGRAM_BOT_TOKEN=//p' "$env_file" 2>/dev/null | tail -1)
  ids=$(sed -n 's/^HS_TELEGRAM_CHAT_IDS=//p' "$env_file" 2>/dev/null | tail -1)
  [ -n "$token" ] && [ -n "$ids" ] || return 0
  for id in ${ids//,/ }; do
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
      | curl -s -m 10 -o /dev/null -K - --data-urlencode "chat_id=$id" --data-urlencode "text=$1" || true
  done
}

# Новая версия здорова, если панель отвечает в течение полутора минут, а бот
# после этого полминуты работает без перезапусков.
healthy() {
  local i restarts
  for i in $(seq 1 30); do
    curl -fs -m 3 -o /dev/null "$HEALTH_URL" && break
    [ "$i" = 30 ] && return 1
    sleep 3
  done
  restarts=$(systemctl show -p NRestarts --value homesec-bot)
  sleep 30
  systemctl is-active --quiet homesec homesec-bot \
    && [ "$(systemctl show -p NRestarts --value homesec-bot)" = "$restarts" ]
}

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

if [ "$remote_sha" = "$(cat "$REJECTED_FILE" 2>/dev/null || true)" ]; then
  exit 0   # этот коммит уже откатывали; ждём следующий
fi

# Гейт на зелёный CI. Ставим только коммит, у которого все проверки GitHub
# завершились успешно. Нет ответа, лимит запросов, проверки ещё идут — ждём
# следующего тика. Раньше здесь сравнивались строки без пробела после
# двоеточия, а GitHub отвечает с пробелом, поэтому гейт не срабатывал никогда.
if [ "${CHECK_CI:-1}" = "1" ]; then
  ci_api="https://api.github.com/repos/nzulkaynarov/homesec/commits/$remote_sha/check-runs"
  verdict=$(curl -s -m 10 -H "Accept: application/vnd.github+json" "$ci_api" 2>/dev/null \
    | python3 -I "$APP_DIR/deploy/ci_verdict.py" || echo wait)
  case "$verdict" in
    green) ;;
    red)
      if [ "$(cat "$STATE_DIR/ci_red_sha" 2>/dev/null || true)" != "$remote_sha" ]; then
        echo "$(date -Is) homesec update: CI для $remote_sha упал, коммит не ставим" >&2
        mkdir -p "$STATE_DIR" && echo "$remote_sha" > "$STATE_DIR/ci_red_sha"
        notify "⚠️ HomeSec: новая версия не прошла проверки, на Pi осталась прежняя."
      fi
      exit 0 ;;
    *) exit 0 ;;
  esac
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

# Синхронизация переменных из GitHub (backend/conf.d/homesec.env) в локальный .env
if [ -f "$APP_DIR/backend/conf.d/homesec.env" ]; then
  env_file="$APP_DIR/backend/.env"
  [ -f "$env_file" ] || touch "$env_file"
  while IFS="=" read -r key val || [ -n "$key" ]; do
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${key// /}" ]] && continue
    key="${key// /}"
    if [ -n "$key" ] && [ -n "$val" ]; then
      if grep -q "^${key}=" "$env_file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$env_file"
      else
        echo "${key}=${val}" >> "$env_file"
      fi
    fi
  done < "$APP_DIR/backend/conf.d/homesec.env"
fi

systemctl restart homesec
systemctl restart homesec-bot

if ! healthy; then
  echo "$(date -Is) homesec update: $remote_sha не поднялся, откат на ${deployed_sha:-?}" >&2
  mkdir -p "$STATE_DIR" && echo "$remote_sha" > "$REJECTED_FILE"
  if [ -n "$deployed_sha" ]; then
    run git -C "$APP_DIR" reset --hard "$deployed_sha"
    sync_units
    systemctl restart homesec
    systemctl restart homesec-bot
  fi
  notify "⚠️ HomeSec: новая версия не запустилась на Pi, вернул прежнюю. Правила работают по-старому."
  exit 1
fi

# Метка успеха — последней строкой. Всё, что выше, при сбое ретраится.
mkdir -p "$STATE_DIR"
echo "$remote_sha" > "$DEPLOYED_FILE"
echo "$(date -Is) homesec restarted, deployed $remote_sha"
