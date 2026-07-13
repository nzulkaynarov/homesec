# ============================================================================
# HomeSec — включение контроля через AdGuard (шаг 3, ПОСЛЕ homesec-base.rsc)
#
# Запускать ТОЛЬКО когда Raspberry Pi уже подключена к MikroTik и доступна
# на 192.168.88.2 с работающим AdGuard Home. Проверь заранее в терминале:
#     /ping 192.168.88.2 count=3
# Пинг идёт — можно импортировать. Не идёт — сначала подключи малинку.
#
# Импорт:  /import file-name=enable-adguard.rsc
#
# Что делает:
#   - переводит DNS всей сети на малинку (AdGuard);
#   - включает принудительный заворот порта 53 (обход сменой DNS перестаёт
#     работать). После этого дети не сменят DNS на 8.8.8.8 в обход фильтра.
#
# Откат (если что-то не так — вернуть DNS на сам роутер):
#   /ip dhcp-server network set [find address=192.168.88.0/24] dns-server=192.168.88.1
#   /ip dns set servers=1.1.1.1,8.8.8.8
#   /ip firewall nat disable [find comment~"hs: force DNS"]
#   /ip firewall nat disable [find comment~"hs: hairpin DNS"]
# ============================================================================

:local piAddr "192.168.88.2"

# Проверка доступности малинки перед включением
:if ([/ping $piAddr count=2] = 0) do={
  :error "Малинка $piAddr недоступна — сначала подключи её к MikroTik. Контроль НЕ включён."
}

# 1. Клиенты получают DNS = AdGuard на малинке
/ip dhcp-server network set [find address=192.168.88.0/24] dns-server=$piAddr
# 2. Сам роутер тоже резолвит через AdGuard (единый лог запросов)
/ip dns set servers=$piAddr
# 3. Включаем принудительный заворот DNS + hairpin (порт 53 -> AdGuard)
/ip firewall nat enable [find comment="hs: force DNS -> AdGuard"]
/ip firewall nat enable [find comment="hs: hairpin DNS"]

:put "AdGuard-контроль включён. DNS всей сети идёт через 192.168.88.2."
