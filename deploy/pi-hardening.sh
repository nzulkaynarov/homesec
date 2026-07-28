#!/usr/bin/env bash
# Хардненинг малинки после ЧП 2026-07-27 (Pi зависла намертво, дом остался
# без DNS). Скрипт идемпотентный — можно гонять повторно.
#
#   sudo bash /opt/homesec/deploy/pi-hardening.sh --diagnose   # только собрать улики
#   sudo bash /opt/homesec/deploy/pi-hardening.sh              # применить меры
#
# Что делают меры:
#   1) аппаратный watchdog — Pi сама перезагружается, если зависла;
#   2) потолок журналов systemd — логи больше не могут забить SD-карту;
#   3) zram-swap — запас памяти на 1 ГБ без износа карты;
#   4) обрезка хранения журнала AdGuard (главный подозреваемый по росту).
# Лимиты памяти самим сервисам (MemoryMax) живут в deploy/homesec*.service и
# приезжают обычным деплоем.
set -euo pipefail

MODE="${1:-apply}"
APP_DIR="${APP_DIR:-/opt/homesec}"

if [ "$(id -u)" != 0 ]; then
  echo "Запускать через sudo: sudo bash $0 $*" >&2
  exit 1
fi

hr() { echo; echo "===== $* ====="; }

# ---------------------------------------------------------------------------
# Диагностика: всё, что нужно знать о причине зависания, одной командой.
# ---------------------------------------------------------------------------
diagnose() {
  hr "Аптайм и загрузки"
  uptime
  journalctl --list-boots 2>/dev/null | tail -5 || echo "журнал загрузок недоступен"

  hr "Убийца по памяти (OOM) в ПРЕДЫДУЩУЮ загрузку"
  if journalctl -b -1 -k --no-pager 2>/dev/null | grep -iE "out of memory|oom-kill|killed process" | tail -20; then
    :
  else
    echo "следов OOM в прошлой загрузке не найдено"
  fi

  hr "Ошибки и предупреждения перед падением (последние 40 строк прошлой загрузки)"
  journalctl -b -1 -p warning --no-pager 2>/dev/null | tail -40 || echo "журнал прошлой загрузки недоступен"

  hr "Место на диске"
  df -h / /var/log 2>/dev/null || df -h
  echo "-- крупнейшие каталоги логов и данных --"
  du -sh /var/log 2>/dev/null || true
  du -sh /opt/AdGuardHome/data/* 2>/dev/null | sort -h | tail -5 || true

  hr "Память сейчас"
  free -h
  echo "-- топ-5 процессов по памяти --"
  ps -eo pid,comm,rss --sort=-rss | head -6

  hr "Сервисы HomeSec"
  systemctl --no-pager --lines=0 status homesec homesec-bot AdGuardHome 2>/dev/null | grep -E "●|Active:|Memory:" || true

  hr "DNS — то, что реально отказало в ЧП 2026-07-27"
  if bash "$APP_DIR/deploy/dns-selfheal.sh" --dry-run 2>/dev/null; then
    echo "DNS отвечает"
  fi
  systemctl is-active homesec-dnscheck.timer >/dev/null 2>&1 \
    && echo "сторож DNS (homesec-dnscheck.timer): включён" \
    || echo "сторож DNS (homesec-dnscheck.timer): ВЫКЛЮЧЕН — приедет с деплоем"
  journalctl -t homesec-dnscheck --no-pager -n 5 2>/dev/null | tail -5

  hr "Watchdog"
  if [ -e /dev/watchdog ]; then echo "/dev/watchdog есть"; else echo "/dev/watchdog НЕТ — нужен dtparam=watchdog=on и перезагрузка"; fi
  grep -hE "^RuntimeWatchdogSec" /etc/systemd/system.conf /etc/systemd/system.conf.d/*.conf 2>/dev/null || echo "RuntimeWatchdogSec не задан"
}

if [ "$MODE" = "--diagnose" ] || [ "$MODE" = "-d" ]; then
  diagnose
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Аппаратный watchdog: зависшая малинка перезагрузится сама.
# ---------------------------------------------------------------------------
hr "1/4 Аппаратный watchdog"
if [ ! -e /dev/watchdog ]; then
  added=0
  for cfg in /boot/firmware/config.txt /boot/config.txt; do
    if [ -f "$cfg" ] && ! grep -q '^dtparam=watchdog=on' "$cfg"; then
      echo 'dtparam=watchdog=on' >> "$cfg"
      echo "  включил watchdog в $cfg"
      added=1
    fi
  done
  [ "$added" = 1 ] && echo "  ⚠️  заработает ПОСЛЕ перезагрузки малинки"
  [ "$added" = 0 ] && echo "  ⚠️  /dev/watchdog нет и config.txt не найден — проверь модуль bcm2835_wdt вручную"
else
  echo "  /dev/watchdog на месте"
fi
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/10-homesec-watchdog.conf <<'EOF'
# Если systemd перестал гладить watchdog 15 секунд — ядро перезагружает Pi.
# Ответ на ЧП 2026-07-27: зависшая малинка = весь дом без DNS.
[Manager]
RuntimeWatchdogSec=15
RebootWatchdogSec=2min
EOF
systemctl daemon-reexec
echo "  RuntimeWatchdogSec=15 применён"

# ---------------------------------------------------------------------------
# 2. Журналы systemd не должны съедать карту.
# ---------------------------------------------------------------------------
hr "2/4 Потолок журналов systemd"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/10-homesec.conf <<'EOF'
[Journal]
SystemMaxUse=64M
SystemMaxFileSize=8M
MaxRetentionSec=1month
EOF
systemctl restart systemd-journald
journalctl --vacuum-size=64M >/dev/null 2>&1 || true
echo "  журнал ограничен 64 МБ"

# ---------------------------------------------------------------------------
# 3. zram-swap: запас памяти без износа SD.
# ---------------------------------------------------------------------------
hr "3/4 zram-swap"
if ! dpkg -s zram-tools >/dev/null 2>&1; then
  if apt-get install -y -qq zram-tools >/dev/null 2>&1; then
    echo "  zram-tools установлен"
  else
    echo "  ⚠️  не удалось поставить zram-tools (нет сети?) — шаг пропущен"
  fi
fi
if [ -f /etc/default/zramswap ]; then
  sed -i 's/^#\?ALGO=.*/ALGO=lz4/; s/^#\?PERCENT=.*/PERCENT=50/' /etc/default/zramswap
  systemctl restart zramswap 2>/dev/null || systemctl restart zramswap.service 2>/dev/null || true
  echo "  zram: lz4, 50% RAM"
  swapon --show 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 4. AdGuard: не хранить журнал запросов 90 дней.
# ---------------------------------------------------------------------------
hr "4/4 Хранение журнала AdGuard"
if [ -f "$APP_DIR/deploy/adguard_retention.py" ]; then
  python3 "$APP_DIR/deploy/adguard_retention.py" --apply || \
    echo "  ⚠️  не вышло — задай в веб-интерфейсе AdGuard: Settings → Query log → 24 hours, Statistics → 7 days"
else
  echo "  скрипт adguard_retention.py не найден — задай retention в веб-интерфейсе AdGuard"
fi

hr "Готово"
echo "Проверить состояние: sudo bash $0 --diagnose"
echo "Лимиты памяти сервисам (MemoryMax) приезжают обычным деплоем из deploy/*.service."
