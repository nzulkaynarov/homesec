# CLAUDE.md — контекст для ИИ-агентов

HomeSec — работающая система контроля домашней сети. Это НЕ учебный проект:
изменения в `main` автоматически деплоятся в реальную домашнюю сеть семьи.
Ошибка в правилах DNS/firewall оставляет дом без интернета.

## Текущее состояние (2026-07-14): РАЗВЁРНУТО И РАБОТАЕТ

```text
Оптика ── Huawei HG8546M (роутер, 192.168.0.1, режим БЕЗ моста — осознанно)
              │ LAN
          MikroTik hAP ac2 (ether1 ← DHCP 192.168.0.161; LAN 192.168.88.1/24)
              │  RouterOS 7.23.2 + firmware 7.23.2, admin известен владельцу
              ├─ Raspberry Pi 3B  192.168.88.2 (Debian 12, ssh: znz)
              │    ├─ AdGuard Home  :53 DNS, :3000 веб (admin)
              │    └─ панель HomeSec :8000 (systemd homesec) + pull-деплой
              ├─ Archer C6 (режим AP — семейный Wi-Fi 2.4/5)
              └─ Wi-Fi MikroTik: wlan2 5ГГц "Cherkash-5G" (осн., канал 36)
                                 wlan1 2.4ГГц "Podliva" (ГОСТЕВАЯ, в bridge-guest)
```

- Принудительный DNS включён: mark-connection `hs-dns` + dst-nat на Pi +
  **hairpin masquerade** (см. «Выстраданные правила» №3).
- Гостевая сеть: 192.168.90.0/24, изоляция от LAN, лимит 20М (queue hs-guest-limit).
- Панель управляет ТОЛЬКО address-list'ами `hs-*` и очередями `hs-dev-*`.
  Firewall/NAT/mangle-правила она не создаёт и не удаляет — их ставит .rsc.

## Секреты — В РЕПОЗИТОРИИ ИХ НЕТ

Все пароли лежат в `/opt/homesec/backend/.env` на малинке (chmod 600):
`ssh znz@192.168.88.2` (пароль знает владелец) → `cat /opt/homesec/backend/.env`.
Там же: пароль панели (HS_ADMIN_PASSWORD), AdGuard (HS_ADGUARD_*), API-пользователь
MikroTik `homesec` (HS_MIKROTIK_PASSWORD). Доступ MikroTik: `ssh admin@192.168.88.1`
(пароль у владельца; для старого host-key нужен `-o HostKeyAlgorithms=+ssh-rsa`).
Никогда не коммить пароли, `.env`, `mikrotik/homesec-configured.rsc` (в .gitignore).

## Рабочий процесс

- Тесты: `cd backend && pytest` (16 тестов; интеграции замоканы недоступными портами).
- CI: GitHub Actions на каждый push (`.github/workflows/ci.yml`).
- **CD: push в `main` = деплой в прод.** Малинка раз в минуту тянет `main`
  (systemd `homesec-update.timer` → `deploy/update.sh`, read-only deploy key)
  и перезапускает панель. Рискованные изменения — через ветку, в main только зелёное.
- Работать с сетью можно только изнутри неё (панель за NAT, извне не доступна).

## Выстраданные правила (каждое — реальный инцидент)

1. **Не включать принудительный DNS, пока Pi не отвечает на 192.168.88.2.**
   Иначе весь DNS дома уходит в никуда → «нет интернета». Поэтому импорт
   `homesec-base.rsc` создаёт заворот ВЫКЛЮЧЕННЫМ, а включает его отдельный
   `enable-adguard.rsc` с проверкой пинга малинки.
2. **IP самой малинки никогда не попадает в hs-managed/hs-blocked.**
   Она инфраструктура: её upstream-запросы (AdGuard→1.1.1.1) нельзя резать.
   Защита в коде: `get_self_ips()` в `backend/app/services/enforcement.py` — не удалять.
3. **Заворот DNS в той же подсети требует hairpin masquerade.** Простой dst-nat
   ломается: клиент слал на 8.8.8.8, ответ приходит с 88.2 → «reply from
   unexpected source», отвергается (симптом: часть сайтов не открывается).
   Схема: mangle mark `hs-dns` → dst-nat → srcnat masquerade по метке.
4. **Блокировка DoH — только tcp/udp 443 к списку hs-doh.** В hs-doh входят
   1.1.1.1/8.8.8.8; блокировать к ним ВЕСЬ трафик = зарезать upstream AdGuard.
5. **`.rsc`-скрипты одноразовые** (не идемпотентны): повторный импорт создаёт
   дубли правил. Проверять `print where comment~"hs:"` перед импортом.
6. **Легаси-драйвер wireless (7.x) врёт в свойстве `running`** (false при
   вещающем радио). Истина — в `/interface wireless monitor <iface> once`.
7. **Firmware (RouterBOOT) должен совпадать с версией RouterOS.** Рассинхрон
   (7.11 vs 7.23.2) давал обрыв WPA2-рукопожатия («unicast key exchange
   timeout»). Лечение: `/system routerboard upgrade` + reboot.
8. **5ГГц не поднимается — проверь DFS.** `frequency=auto` может выбрать
   радарный канал и застрять в radar-detecting. Ставить non-DFS: 5180 (36).

## Карта кода

| Путь | Что это |
|---|---|
| `backend/app/services/enforcement.py` | ЯДРО: расчёт желаемого состояния + reconcile (раз в минуту и после каждого изменения) |
| `backend/app/services/mikrotik.py` | RouterOS API (librouteros); только address-lists, queues, leases, connections |
| `backend/app/services/adguard.py` | AdGuard REST API: per-client блокировки сервисов, safe search |
| `backend/app/ai/tools.py` | Реестр инструментов — ЕДИНСТВЕННАЯ точка, через которую ИИ и бот трогают систему; guardrails зашиты в код (self-IP, только hs-*, аудит) |
| `backend/app/ai/` | ИИ-слой: client.py (Claude API + дневной бюджет), orchestrator.py (NL→tools, мутации только через кнопку), analyst.py (дайджест), watchdog.py (аномалии: эвристики + LLM-оформление) |
| `backend/app/bot/` | Telegram-бот, отдельный systemd-юнит homesec-bot: уведомления, health-алерты, команды, ИИ-канал |
| `backend/app/migrations.py` | Мини-миграции схемы (PRAGMA user_version), только аддитивные |
| `backend/app/routers/`, `templates/` | Веб-панель (FastAPI + Jinja2, без JS-фреймворков) |
| `mikrotik/homesec-base.rsc` | Базовый импорт (контроль создаётся выключенным) |
| `mikrotik/enable-adguard.rsc` | Отдельное включение принудительного DNS (с проверкой Pi) |
| `deploy/` | systemd-юниты + install.sh + pull-деплой update.sh |
| `docs/05-operations.md` | Снапшот прода, health-чеки, откаты — читать перед работой с живой сетью |
| `docs/06-ai-multiagent-tz.md` | ТЗ ИИ-слоя: архитектура, guardrails, этапы (реализованы этапы 0–2) |

## Дорожная карта (согласована с владельцем)

Фаза 1 — **СДЕЛАНО**: Telegram-бот (уведомления о новых устройствах с
кнопками, /status /block /pause /resume /digest, health-алерты). Для запуска
на проде нужно заполнить HS_TELEGRAM_* в .env на малинке.
Фаза 3 (ИИ) — **СДЕЛАНО в базовой версии**: NL-управление через Claude API
(app/ai/orchestrator.py; Opus для рассуждений, Haiku для рутины), детектор
аномалий (ночная активность, всплеск DoH), ИИ-дайджест дня. Правила: любая
мутация от ИИ — только через кнопку подтверждения в Telegram; дневной бюджет
токенов HS_AI_DAILY_TOKEN_BUDGET; без ключа HS_ANTHROPIC_API_KEY ИИ молчит,
остальное работает.
Осталось (фаза 2): квоты времени по категориям, страница «время вышло»,
портал регистрации неизвестных устройств, эвристики против MAC-рандомизации.

Известный продуктовый риск: MAC-рандомизация телефонов плодит «новые
устройства» — учитывать в дизайне идентификации (фаза 2).

## Владелец

Общение — на русском. Малинка перепрофилирована под HomeSec с его согласия
(старые сервисы gradesentinel_bot/motionEye/n8n остановлены, docker-данные
сохранены на диске). Huawei в мост переводить пока НЕ хочет. Постоянные
SSH-ключи агента на устройства не ставить — только парольный доступ по сессии.
