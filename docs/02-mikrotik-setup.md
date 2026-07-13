# Шаг 2. Настройка MikroTik hAP ac2

Исходное состояние: заводская defconf-конфигурация (как в вашем экспорте) —
bridge на ether2–5 + wlan1/2, LAN 192.168.88.0/24, DHCP-клиент на ether1.

## 1. Обновите RouterOS И firmware загрузчика (важно)

Winbox → System → Packages → Check For Updates (канал stable). После обновления
ОС обязательно синхронизируйте firmware загрузчика — рассинхрон ломает Wi-Fi
(клиенты отваливаются с «unicast key exchange timeout», проверено на опыте):

```routeros
/system routerboard upgrade
/system reboot
```

## 2. Поднимите интернет на ether1

### Вариант A: провайдер выдаёт адрес по DHCP

Уже работает — defconf содержит DHCP-клиент на ether1. Проверьте:

```routeros
как
/ping 8.8.8.8
```

### Вариант B: PPPoE (логин/пароль от провайдера)

```routeros
/interface pppoe-client add name=pppoe-wan interface=ether1 user=ВАШ_ЛОГИН password=ВАШ_ПАРОЛЬ use-peer-dns=no add-default-route=yes disabled=no
/interface list member add interface=pppoe-wan list=WAN
/ip dhcp-client disable [find interface=ether1]
```

### Если провайдер использует VLAN на WAN (часто на GPON)

```routeros
/interface vlan add name=wan-vlan interface=ether1 vlan-id=ВАШ_VLAN_ID
```

и поднимайте DHCP-клиент или PPPoE поверх `wan-vlan` (не ether1),
добавив `wan-vlan` в list=WAN.

## 3. Смените пароль администратора и SSID

```routeros
/user set admin password=СИЛЬНЫЙ_ПАРОЛЬ
/interface wireless security-profiles set [find default=yes] mode=dynamic-keys authentication-types=wpa2-psk wpa2-pre-shared-key=ПАРОЛЬ_WIFI
/interface wireless set wlan1 ssid=Home
/interface wireless set wlan2 ssid=Home-5G
```

## 4. Импортируйте базовую конфигурацию HomeSec

1. Откройте `mikrotik/homesec-base.rsc`, замените
   `CHANGE_ME_API_PASSWORD` (пароль API-пользователя для панели) и
   `CHANGE_ME_GUEST_WIFI` (пароль гостевого Wi-Fi).
2. Winbox → Files → перетащите файл на роутер.
3. В терминале:

```routeros
/import file-name=homesec-base.rsc
```

Скрипт создаст: API-пользователя `homesec` (доступ только с 192.168.88.2),
блокировки DoT/DoH и правила для списков `hs-*`, гостевую сеть 192.168.90.0/24
с изоляцией и лимитом 20 Мбит/с.

⚠️ **Принудительный DNS создаётся ВЫКЛЮЧЕННЫМ — это нормально.** Сразу после
импорта сеть работает по-старому (DNS резолвит сам роутер). Так задумано:
включать заворот DNS до того, как малинка встала на 192.168.88.2, нельзя —
весь дом останется без DNS. Импорт одноразовый: повторный запуск создаст дубли.

## 5. Закрепите адрес Raspberry Pi

Pi должен всегда иметь 192.168.88.2 (на него будет заведён DNS всей сети):

```routeros
/ip dhcp-server lease add address=192.168.88.2 mac-address=MAC_ВАШЕГО_PI server=defconf comment="hs: raspberry-pi"
```

MAC можно посмотреть в `/ip dhcp-server lease print` пока Pi подключён.

## 6. Включите контроль (после шага 3 — установки на Pi)

Когда AdGuard и панель на малинке работают (`docs/03`), а `/ping 192.168.88.2`
с роутера проходит — залейте и импортируйте `mikrotik/enable-adguard.rsc`:

```routeros
/import file-name=enable-adguard.rsc
```

Скрипт сам проверит доступность малинки и только тогда переведёт DNS всей
сети на AdGuard (mark-based dst-nat + hairpin masquerade). Откат описан
в шапке скрипта и в `docs/05-operations.md`.

## Что делает конфигурация (для понимания)

| Механизм | Как работает |
|---|---|
| Принудительный DNS | mangle помечает пакеты на порт 53 (метка `hs-dns`) → dst-nat на 192.168.88.2 → **hairpin masquerade** (без него ответ приходит «с неожиданного источника» и отвергается клиентом — AdGuard в той же подсети). Ребёнок ставит 8.8.8.8 — запрос всё равно приходит в AdGuard. Малинка исключена (её upstream трогать нельзя) |
| Блок DoT/DoQ | drop tcp/udp 853 для управляемых устройств |
| Блок DoH | drop **только tcp/udp 443** к списку `hs-doh` (наполняет панель). Весь трафик к этим IP блокировать нельзя: среди них 1.1.1.1/8.8.8.8 — upstream AdGuard |
| Блокировка устройства | IP в списке `hs-blocked` → forward drop. Панель дополнительно рвёт активные соединения |
| Fasttrack | Исключены устройства из `hs-managed`, иначе лимиты скорости и мгновенные блокировки не работали бы |
| Гостевая сеть | Отдельный SSID → bridge-guest → 192.168.90.0/24, в домашнюю сеть нельзя (кроме DNS на Pi), скорость ограничена |

## Проверка

```routeros
/ip firewall filter print where comment~"hs:"
/ip firewall nat print where comment~"hs:"
/interface wireless print
```

С телефона: подключитесь к `Home-Guest` — интернет есть,
`http://192.168.88.1` недоступен.
