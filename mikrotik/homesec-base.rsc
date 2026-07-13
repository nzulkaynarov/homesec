# ============================================================================
# HomeSec — базовая конфигурация RouterOS (проверено на 7.11, hAP ac2)
#
# ПЕРЕД ИМПОРТОМ:
#   1. Настройте интернет на MikroTik (docs/02-mikrotik-setup.md).
#   2. Замените CHANGE_ME_API_PASSWORD и CHANGE_ME_GUEST_WIFI ниже.
#   3. Убедитесь, что Raspberry Pi будет иметь адрес 192.168.88.2
#      (панель сама закрепит lease, либо задайте статику на Pi).
#
# Импорт: залейте файл через Winbox (Files) и выполните:
#   /import file-name=homesec-base.rsc
#
# Скрипт идемпотентен НЕ полностью — запускайте один раз на чистой
# defconf-конфигурации. Повторный запуск создаст дубли правил.
# ============================================================================

:local piAddr "192.168.88.2"

# ----------------------------------------------------------------------------
# 1. Пользователь API для панели HomeSec (доступ только с Pi)
# ----------------------------------------------------------------------------
/user group add name=hs-api policy=api,read,write,test,!local,!telnet,!ssh,!ftp,!reboot,!policy,!winbox,!password,!web,!sniff,!sensitive,!romon,!rest-api
/user add name=homesec group=hs-api password=CHANGE_ME_API_PASSWORD address=$piAddr comment="HomeSec panel"
/ip service set api address=$piAddr disabled=no
/ip service set www address=192.168.88.0/24
/ip service set winbox address=192.168.88.0/24

# ----------------------------------------------------------------------------
# 2. DNS — БЕЗОПАСНО ПО УМОЛЧАНИЮ.
#    Пока НЕ переводим клиентов на малинку: сеть должна работать ДО того, как
#    Raspberry Pi окажется на 192.168.88.2. Резолвит сам MikroTik через upstream.
#    Перевод DNS на AdGuard делается ОТДЕЛЬНО (enable-adguard.rsc) — только после
#    того, как малинка подтверждённо доступна на 192.168.88.2.
#    (DHCP по defconf уже выдаёт клиентам dns-server=192.168.88.1 — сам роутер.)
# ----------------------------------------------------------------------------
/ip dns set servers=1.1.1.1,8.8.8.8 allow-remote-requests=yes

# ----------------------------------------------------------------------------
# 3. Принудительный DNS на AdGuard — заворот порта 53 на малинку.
#    ВЫКЛЮЧЕНО по умолчанию (disabled=yes): включится в enable-adguard.rsc,
#    иначе при отсутствующей малинке весь DNS уходит в никуда и «нет интернета».
# ----------------------------------------------------------------------------
/ip firewall nat
add chain=dstnat action=accept src-address=$piAddr comment="hs: Pi resolves upstream freely"
add chain=dstnat action=dst-nat protocol=udp dst-port=53 src-address=192.168.88.0/24 to-addresses=$piAddr comment="hs: force DNS (udp) -> AdGuard" disabled=yes
add chain=dstnat action=dst-nat protocol=tcp dst-port=53 src-address=192.168.88.0/24 to-addresses=$piAddr comment="hs: force DNS (tcp) -> AdGuard" disabled=yes
add chain=dstnat action=dst-nat protocol=udp dst-port=53 src-address=192.168.90.0/24 to-addresses=$piAddr comment="hs: guest force DNS (udp)" disabled=yes
add chain=dstnat action=dst-nat protocol=tcp dst-port=53 src-address=192.168.90.0/24 to-addresses=$piAddr comment="hs: guest force DNS (tcp)" disabled=yes

# ----------------------------------------------------------------------------
# 4. Fasttrack: исключаем контролируемые устройства (список hs-managed),
#    иначе ограничения скорости и мгновенные блокировки не сработают.
#    Панель сама добавляет устройства в hs-managed.
# ----------------------------------------------------------------------------
/ip firewall filter
set [find comment="defconf: fasttrack"] src-address-list=!hs-managed dst-address-list=!hs-managed

# ----------------------------------------------------------------------------
# 5. Правила контроля (вставляются ПЕРЕД fasttrack, иначе установленные
#    соединения проскочат мимо). Списки hs-* наполняет панель.
# ----------------------------------------------------------------------------
/ip firewall filter
add chain=forward action=drop src-address-list=hs-blocked comment="hs: drop blocked devices" place-before=[find comment="defconf: fasttrack"]
add chain=forward action=drop protocol=tcp dst-port=853 src-address-list=hs-managed comment="hs: block DoT (tcp/853)" place-before=[find comment="defconf: fasttrack"]
add chain=forward action=drop protocol=udp dst-port=853 src-address-list=hs-managed comment="hs: block DoT/DoQ (udp/853)" place-before=[find comment="defconf: fasttrack"]
# DoH — ТОЛЬКО порт 443 к DoH-серверам. Нельзя рубить весь трафик к этим IP:
# среди них 1.1.1.1/8.8.8.8, а обычный DNS (порт 53) на них заворачивается на
# AdGuard отдельно. Блокировка всего трафика ломала бы upstream AdGuard.
add chain=forward action=drop protocol=tcp dst-port=443 dst-address-list=hs-doh src-address-list=hs-managed comment="hs: block DoH (tcp/443)" place-before=[find comment="defconf: fasttrack"]
add chain=forward action=drop protocol=udp dst-port=443 dst-address-list=hs-doh src-address-list=hs-managed comment="hs: block DoH (udp/443 DoQ/H3)" place-before=[find comment="defconf: fasttrack"]
add chain=forward action=drop protocol=udp dst-port=443 src-address-list=hs-kids comment="hs: block QUIC for kids" disabled=yes place-before=[find comment="defconf: fasttrack"]
add chain=forward action=drop protocol=udp dst-port=1194,51820 src-address-list=hs-kids comment="hs: block VPN (OpenVPN/WireGuard) for kids" place-before=[find comment="defconf: fasttrack"]

# ----------------------------------------------------------------------------
# 6. Гостевая сеть: отдельный SSID, подсеть 192.168.90.0/24,
#    изоляция от домашней сети, лимит скорости.
# ----------------------------------------------------------------------------
/interface wireless security-profiles
add name=hs-guest mode=dynamic-keys authentication-types=wpa2-psk wpa2-pre-shared-key=CHANGE_ME_GUEST_WIFI supplicant-identity=""
/interface wireless
add name=wlan-guest2 master-interface=wlan1 mode=ap-bridge ssid="Home-Guest" security-profile=hs-guest wps-mode=disabled disabled=no
add name=wlan-guest5 master-interface=wlan2 mode=ap-bridge ssid="Home-Guest" security-profile=hs-guest wps-mode=disabled disabled=no
/interface bridge
add name=bridge-guest
/interface bridge port
add bridge=bridge-guest interface=wlan-guest2
add bridge=bridge-guest interface=wlan-guest5
/ip address
add address=192.168.90.1/24 interface=bridge-guest network=192.168.90.0
/ip pool
add name=guest-pool ranges=192.168.90.10-192.168.90.254
/ip dhcp-server
add name=guest-dhcp interface=bridge-guest address-pool=guest-pool lease-time=1h disabled=no
/ip dhcp-server network
add address=192.168.90.0/24 gateway=192.168.90.1 dns-server=$piAddr comment="hs: guest network"

# Гости: к роутеру — только DHCP, в домашнюю сеть — только DNS на Pi
/ip firewall filter
add chain=input action=accept protocol=udp dst-port=67 in-interface=bridge-guest comment="hs: guest DHCP" place-before=[find comment="defconf: drop all not coming from LAN"]
add chain=input action=drop in-interface=bridge-guest comment="hs: guest no router access" place-before=[find comment="defconf: drop all not coming from LAN"]
add chain=forward action=accept src-address=192.168.90.0/24 dst-address=$piAddr protocol=udp dst-port=53 comment="hs: guest DNS to AdGuard" place-before=[find comment="hs: drop blocked devices"]
add chain=forward action=accept src-address=192.168.90.0/24 dst-address=$piAddr protocol=tcp dst-port=53 comment="hs: guest DNS to AdGuard tcp" place-before=[find comment="hs: drop blocked devices"]
add chain=forward action=drop src-address=192.168.90.0/24 dst-address=192.168.88.0/24 comment="hs: guest isolation" place-before=[find comment="hs: drop blocked devices"]

# Лимит скорости для всей гостевой сети (панель может менять)
/queue simple
add name=hs-guest-limit target=192.168.90.0/24 max-limit=20M/20M comment="hs: guest bandwidth"

# ----------------------------------------------------------------------------
# 7. Закрепляем Pi статическим lease (если он уже получил адрес по DHCP,
#    отредактируйте руками; панель также умеет закреплять устройства)
# ----------------------------------------------------------------------------
# /ip dhcp-server lease add address=192.168.88.2 mac-address=XX:XX:XX:XX:XX:XX server=defconf comment="hs: raspberry-pi"

:put "HomeSec base config imported. Now change the 'homesec' user password if you kept the placeholder!"
