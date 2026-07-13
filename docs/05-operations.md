# Эксплуатация: текущее состояние и runbook

Снапшот на 2026-07-14. Система развёрнута и работает в режиме БЕЗ моста.

## Адреса и доступы

| Что | Где | Доступ |
|---|---|---|
| MikroTik hAP ac2 | 192.168.88.1 | ssh/winbox `admin` (пароль у владельца); API-юзер `homesec` только с Pi |
| Raspberry Pi 3B | 192.168.88.2 | `ssh znz@192.168.88.2` |
| Панель HomeSec | http://192.168.88.2:8000 | `admin` / см. `.env` |
| AdGuard Home | http://192.168.88.2:3000 | `admin` / см. `.env` |
| Секреты | `/opt/homesec/backend/.env` на Pi | chmod 600, в гите нет |
| WAN | ether1 ← DHCP от Huawei (192.168.0.1) | двойной NAT, это осознанно |

Wi-Fi MikroTik: **Cherkash-5G** (5ГГц, канал 36, основная), **Podliva**
(2.4ГГц = радио wlan1, ГОСТЕВАЯ: порт в bridge-guest, сеть 192.168.90.0/24,
изоляция, 20 Мбит/с). Семейный 2.4ГГц — на Archer C6 (режим AP).

## Health-check (быстрая проверка «всё ли живо»)

С любого устройства в сети:

```bash
ping -c2 192.168.88.1        # роутер
ping -c2 192.168.88.2        # малинка
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.88.2:8000/login   # 200
nslookup google.com          # резолв через AdGuard
nslookup example.com 8.8.8.8 # АНТИ-ОБХОД: должен ответить (hairpin через AdGuard)
```

На Pi: `systemctl status homesec homesec-update.timer AdGuardHome`.
Логи панели: `journalctl -u homesec -f`. Деплой: `journalctl -u homesec-update -f`.

На MikroTik: `/ip firewall nat print where comment~"hs:"` — должно быть ровно
2 правила (hairpin DNS + force DNS -> AdGuard), оба активны.
Wi-Fi: `/interface wireless monitor wlan2 once` (свойству `running` не верить).

## Аварийный откат DNS (дом без интернета, виновата фильтрация)

В терминале winbox/ssh MikroTik — возвращает DNS на роутер, минуя малинку:

```routeros
/ip firewall nat disable [find comment~"hs:"]
/ip dhcp-server network set [find address=192.168.88.0/24] dns-server=192.168.88.1
/ip dns set servers=1.1.1.1,8.8.8.8
```

Обратно (малинка должна пинговаться!): импорт `mikrotik/enable-adguard.rsc`
или `enable` тех же правил + вернуть dns-server=192.168.88.2.

## Частые операции

- **Обновить панель вручную, не дожидаясь таймера:** на Pi
  `sudo systemctl start homesec-update`.
- **Откатить панель на коммит:** `sudo git -C /opt/homesec reset --hard <sha>`
  (следующий tick таймера не перезатрёт: update.sh сравнивает с origin/main —
  для настоящего отката revert'ни коммит в main).
- **Посмотреть, что панель делает с роутером:** таблица «Журнал» в панели
  или `/ip firewall address-list print where list~"hs"` на MikroTik.
- **Перезапуск всего на Pi:** `sudo systemctl restart AdGuardHome homesec`.

## Инциденты, которые уже случались (и их следы)

1. «Нет интернета» после включения контроля → малинка не была на 88.2 /
   не отвечала. Теперь force-DNS включается только скриптом с проверкой пинга.
2. YouTube не открывался при живом google → hairpin-проблема dst-nat в одной
   подсети («reply from unexpected source»). Решение — mark + masquerade, уже в базе.
3. AdGuard отвечал `dns server failure` → его upstream (1.1.1.1) резался нашим
   же DoH-правилом. Теперь DoH блокируется только на 443 и Pi исключена из списков.
4. Wi-Fi «подключается и отваливается» (unicast key exchange timeout) →
   рассинхрон RouterBOOT 7.11 vs RouterOS 7.23.2. `/system routerboard upgrade`.
5. 5ГГц «не появляется» → DFS-канал в radar-detecting. Фикс: frequency=5180.

Бэкап конфига роутера до обновления firmware: файлы `homesec-backup-preupgrade.rsc`
и `homesec-preupgrade.backup` лежат на самом MikroTik (Files).

## Чего НЕ делать

- Не включать `hs: force DNS` при недоступной малинке (весь дом без DNS).
- Не добавлять 192.168.88.2 в hs-managed/hs-blocked руками.
- Не импортировать `.rsc` повторно «на всякий случай» — будут дубли правил.
- Не менять firewall-правила из панели/кода — код управляет только списками
  `hs-*` и очередями `hs-dev-*`, правила принадлежат `.rsc`-скриптам.
- Не пушить в `main` непроверенное — это прод-деплой в течение минуты.
