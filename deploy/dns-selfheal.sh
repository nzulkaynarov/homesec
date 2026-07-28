#!/usr/bin/env bash
# Сторож DNS на самой малинке. Раз в минуту спрашивает у AdGuard имя; если он
# перестал отвечать — перезапускает его.
#
# Зачем (ЧП 2026-07-27): AdGuard перестал отвечать на запросы, но ПРОЦЕСС не
# падал — поэтому Restart=always в юните не сработал ни разу, дом просидел без
# интернета час, и всё закончилось выдёргиванием питания. Пинг малинки в такой
# аварии тоже бесполезен: она жива. Проверять надо саму услугу — резолв имени.
#
# Ставится таймером homesec-dnscheck.timer (приезжает обычным деплоем).
# Ручная проверка: sudo bash /opt/homesec/deploy/dns-selfheal.sh --dry-run
set -uo pipefail

PROBE_NAME="${HS_DNS_PROBE:-example.com}"
STATE_DIR=/var/lib/homesec
LAST_FIX="$STATE_DIR/dns_selfheal_last"
COOLDOWN=600          # не перезапускать AdGuard чаще раза в 10 минут
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# Резолвим через системный резолвер (/etc/resolv.conf → сама малинка), то есть
# проверяем ровно тот путь, которым ходит весь дом. python3 — потому что он
# точно есть (на нём же панель), в отличие от dig, а свой таймаут гарантирует,
# что сторож не зависнет сам.
probe() {
  python3 - "$PROBE_NAME" >/dev/null 2>&1 <<'PY'
import socket, sys
socket.setdefaulttimeout(4)
socket.gethostbyname(sys.argv[1])
PY
}

if probe; then
  exit 0
fi

# Вторая попытка через 5 секунд: одиночный таймаут — не авария.
sleep 5
if probe; then
  exit 0
fi

# Прежде чем трогать AdGuard — проверяем, есть ли вообще связь с роутером.
# В ЧП 2026-07-27 панель полтора часа писала «RouterOS 192.168.88.1:8728
# недоступен»: лежала сеть, а не AdGuard. Перезапускать его в такой ситуации
# бессмысленно (и маскирует настоящую причину) — лучше громко записать факт.
GATEWAY="${HS_GATEWAY:-192.168.88.1}"
if ! ping -c 2 -W 2 "$GATEWAY" >/dev/null 2>&1; then
  msg="DNS не отвечает И роутер $GATEWAY не пингуется — лежит сеть, AdGuard ни при чём"
  if [ "$DRY_RUN" = 1 ]; then echo "$msg"; else logger -t homesec-dnscheck "$msg"; fi
  exit 1
fi

if [ "$DRY_RUN" = 1 ]; then
  echo "DNS не отвечает ($PROBE_NAME), роутер пингуется — в боевом режиме перезапустил бы AdGuardHome"
  exit 1
fi

now=$(date +%s)
last=$(cat "$LAST_FIX" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$COOLDOWN" ]; then
  logger -t homesec-dnscheck "DNS не отвечает, но AdGuard уже перезапускали $(( (now - last) / 60 )) мин назад — жду"
  exit 1
fi

logger -t homesec-dnscheck "DNS не отвечает ($PROBE_NAME) — перезапускаю AdGuardHome"
mkdir -p "$STATE_DIR"
echo "$now" > "$LAST_FIX"
systemctl restart AdGuardHome

sleep 10
if probe; then
  logger -t homesec-dnscheck "AdGuard перезапущен, DNS отвечает"
  exit 0
fi
logger -t homesec-dnscheck "AdGuard перезапущен, но DNS всё ещё молчит — нужен человек"
exit 1
