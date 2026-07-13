# Шаг 2. Настройка MikroTik hAP ac2

Исходное состояние: заводская defconf-конфигурация (как в вашем экспорте) —
bridge на ether2–5 + wlan1/2, LAN 192.168.88.0/24, DHCP-клиент на ether1.

## 1. Обновите RouterOS (рекомендуется)

Winbox → System → Packages → Check For Updates (канал stable).
7.11 подойдёт, но свежие 7.x содержат исправления безопасности.

## 2. Поднимите интернет на ether1

### Вариант A: провайдер выдаёт адрес по DHCP

Уже работает — defconf содержит DHCP-клиент на ether1. Проверьте:

```routeros
/ip dhcp-client print
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
принудительный DNS-редирект на Pi, блокировку DoT/DoH, правила для списков
`hs-*`, гостевую сеть 192.168.90.0/24 с изоляцией и лимитом 20 Мбит/с.

## 5. Закрепите адрес Raspberry Pi

Pi должен всегда иметь 192.168.88.2 (на него заведён DNS всей сети):

```routeros
/ip dhcp-server lease add address=192.168.88.2 mac-address=MAC_ВАШЕГО_PI server=defconf comment="hs: raspberry-pi"
```

MAC можно посмотреть в `/ip dhcp-server lease print` пока Pi подключён.

## Что делает конфигурация (для понимания)

| Механизм | Как работает |
|---|---|
| Принудительный DNS | dstnat: любой пакет на порт 53 → 192.168.88.2 (AdGuard). Ребёнок ставит 8.8.8.8 — запрос всё равно приходит в AdGuard |
| Блок DoT/DoQ | drop tcp/udp 853 для управляемых устройств |
| Блок DoH | drop по списку `hs-doh` (адреса dns.google, cloudflare-dns.com и т.п. добавляет панель) |
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
