# ============================================================================
# HomeSec — АВАРИЙНЫЙ ОТКАТ DNS. Запускать, когда дом остался без интернета
# (малинка мертва/зависла, а весь DNS завёрнут на неё — выстраданное правило №1).
#
# Импорт:  /import file-name=emergency-dns-rollback.rsc
#
# Этот скрипт — ЕДИНСТВЕННЫЙ .rsc, который МОЖНО запускать повторно: он только
# set/disable, ничего не add'ит (правило №5 про дубли к нему не относится).
#
# Что делает:
#   1) выключает все hs-правила NAT (заворот DNS на малинку, перехват страниц);
#   2) возвращает домашней сети DHCP-DNS на сам роутер;
#   3) переводит гостевую сеть на публичный DNS (роутер гостям закрыт правилом
#      «hs: guest no router access», поэтому 192.168.90.1 им резолвером не годится);
#   4) возвращает роутеру публичные upstream и чистит кэш.
#
# Обратно (ТОЛЬКО когда малинка снова пингуется): /import file-name=enable-adguard.rsc
#
# ВАЖНО про селектор: `find address=192.168.88.0/24` на RouterOS 7.23 НЕ матчит
# IP-префикс — find возвращает пусто, set по пустому списку молча ничего не
# делает. Именно поэтому здесь поиск по comment (проверено на проде 2026-07-15)
# с запасным поиском по gateway и явной проверкой результата.
# ============================================================================

:local routerAddr "192.168.88.1"
:local publicDns "1.1.1.1,8.8.8.8"

# --- 1. Выключаем заворот DNS ---------------------------------------------------
# Строго по двум комментариям, а не `find comment~"hs:"`: под общий шаблон
# попадают правила страницы «время вышло», среди которых есть намеренно
# выключенные (портал для hs-unknown) — обратное включение всем скопом подняло
# бы и их. Правила block-page трогать не нужно: они касаются только устройств,
# которые и так заблокированы.
/ip firewall nat disable [find comment="hs: force DNS -> AdGuard"]
/ip firewall nat disable [find comment="hs: hairpin DNS"]
:put "1/4: заворот DNS на малинку выключен."

# --- 2. Домашняя сеть: DHCP раздаёт DNS самого роутера -------------------------
:local lanNets [/ip dhcp-server network find comment=defconf]
:if ([:len $lanNets] = 0) do={
  # Роутер, восстановленный с нуля: comment=defconf может отсутствовать.
  # Запасной путь — сеть, чей шлюз и есть наш роутер.
  :foreach n in=[/ip dhcp-server network find] do={
    :if ([:tostr [/ip dhcp-server network get $n gateway]] = $routerAddr) do={
      :set lanNets ($lanNets, $n)
    }
  }
}
:if ([:len $lanNets] = 0) do={
  :error "Не нашёл домашнюю DHCP-сеть ни по comment=defconf, ни по gateway=$routerAddr. Посмотри '/ip dhcp-server network print' и выставь dns-server=$routerAddr вручную."
}
:foreach n in=$lanNets do={
  /ip dhcp-server network set $n dns-server=$routerAddr
  :local ok false
  :foreach d in=[/ip dhcp-server network get $n dns-server] do={
    :if ([:tostr $d] = $routerAddr) do={ :set ok true }
  }
  :if (!$ok) do={
    :error "DHCP-сеть найдена, но dns-server=$routerAddr не записался. Выставь вручную: /ip dhcp-server network print"
  }
}
:put "2/4: домашняя сеть получает DNS $routerAddr (сетей обновлено: $[:len $lanNets])."

# --- 3. Гостевая сеть: публичный DNS ------------------------------------------
:local guestNets [/ip dhcp-server network find comment="hs: guest network"]
:foreach n in=$guestNets do={
  /ip dhcp-server network set $n dns-server=$publicDns
}
:put "3/4: гостевая сеть получает DNS $publicDns (сетей обновлено: $[:len $guestNets])."

# --- 4. Сам роутер резолвит напрямую ------------------------------------------
/ip dns set servers=$publicDns
/ip dns cache flush
:put "4/4: роутер резолвит через $publicDns, кэш очищен."

:put ""
:put "ОТКАТ ВЫПОЛНЕН. Клиенты подхватят новый DNS при обновлении аренды DHCP"
:put "(до часа). Чтобы быстрее — переподключить Wi-Fi или: /ip dhcp-server lease remove [find dynamic]"
:put "Возврат контроля, когда малинка ожила: /import file-name=enable-adguard.rsc"
