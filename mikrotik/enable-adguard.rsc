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
#   /import file-name=emergency-dns-rollback.rsc
# Тот же откат руками (селектор по comment — `find address=` на 7.23 НЕ матчит
# префикс и молча ничего не делает):
#   /ip firewall nat disable [find comment~"hs:"]
#   /ip dhcp-server network set [find comment=defconf] dns-server=192.168.88.1
#   /ip dhcp-server network set [find comment="hs: guest network"] dns-server=1.1.1.1,8.8.8.8
#   /ip dns set servers=1.1.1.1,8.8.8.8
# ============================================================================

:local piAddr "192.168.88.2"

# Проверка доступности малинки перед включением
:if ([/ping $piAddr count=2] = 0) do={
  :error "Малинка $piAddr недоступна — сначала подключи её к MikroTik. Контроль НЕ включён."
}

# 1. Клиенты получают DNS = AdGuard на малинке.
#    Селектор — по comment: `find address=192.168.88.0/24` на RouterOS 7.23 НЕ
#    матчит IP-префикс (find пуст → set молча no-op). Именно это уже случалось
#    в проде: клиенты продолжали получать DNS роутера, и AdGuard видел всех как
#    один IP — персональные политики детям молча не работали.
:local lanNets [/ip dhcp-server network find comment=defconf]
:if ([:len $lanNets] = 0) do={
  :error "Не нашёл домашнюю DHCP-сеть по comment=defconf. Смотри '/ip dhcp-server network print' — контроль НЕ включён."
}
:foreach n in=$lanNets do={
  /ip dhcp-server network set $n dns-server=$piAddr
  # Самопроверка: убеждаемся, что запись действительно легла.
  :local ok false
  :foreach d in=[/ip dhcp-server network get $n dns-server] do={
    :if ([:tostr $d] = $piAddr) do={ :set ok true }
  }
  :if (!$ok) do={
    :error "DHCP-сеть найдена, но dns-server=$piAddr не записался — контроль НЕ включён."
  }
}
# 2. Сам роутер тоже резолвит через AdGuard (единый лог запросов)
/ip dns set servers=$piAddr
# 3. Включаем принудительный заворот DNS + hairpin (порт 53 -> AdGuard)
/ip firewall nat enable [find comment="hs: force DNS -> AdGuard"]
/ip firewall nat enable [find comment="hs: hairpin DNS"]

:put "AdGuard-контроль включён. DNS всей сети идёт через 192.168.88.2."
