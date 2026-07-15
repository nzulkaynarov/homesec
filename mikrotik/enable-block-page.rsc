# ============================================================================
# HomeSec — страница «время вышло» (опциональный шаг, ПОСЛЕ homesec-base.rsc)
#
# Что делает: HTTP-запросы (порт 80) устройств из списка hs-blocked
# заворачиваются на панель (192.168.88.2:8000) — вместо вечно грузящейся
# страницы человек видит причину блокировки и остаток квоты.
#
# ОГРАНИЧЕНИЕ (осознанное): HTTPS не перехватывается — для этого пришлось бы
# подменять сертификаты. HTTPS-сайты у заблокированного просто не откроются,
# как и раньше. Страницу видно по любому http:// переходу и напрямую:
# http://192.168.88.2:8000/blocked
#
# HAIRPIN (выстраданное правило №3): Pi в той же подсети, что и клиенты,
# поэтому завёрнутый dst-nat'ом HTTP требует masquerade — иначе SYN-ACK
# приходит клиенту напрямую с 88.2:8000 вместо ожидаемого сайта и
# отбрасывается («reply from unexpected source»), страница никогда не
# откроется. Masquerade скрывает от панели IP клиента, поэтому панель
# отвечает на перехваченный запрос редиректом на свой прямой адрес —
# прямое соединение идёт мимо NAT и приходит с настоящим IP.
#
# Реализация hairpin — через mark-connection (как DNS-hairpin в base):
# connection-nat-state=dstnat в srcnat не принимается RouterOS 7.23.
# Помечаем перехваченный HTTP в mangle ДО dst-nat и маскируем по метке —
# прямые заходы на панель (dst-port 8000, не 80) не метятся и IP сохраняют.
#
# Второе правило (hs-unknown, ВЫКЛЮЧЕНО) — для портала регистрации:
# включайте ТОЛЬКО если HS_BLOCK_UNKNOWN=true в .env панели. Иначе оно
# заворачивало бы HTTP незаблокированных «неизвестных» устройств и ломало
# бы им интернет.
#
# Импорт:  /import file-name=enable-block-page.rsc
#
# Откат:
#   /ip firewall nat disable [find comment~"hs: block page"]
#   /ip firewall filter disable [find comment~"hs: allow block page"]
# ============================================================================

:local piAddr "192.168.88.2"

# Защита от повторного импорта (скрипты hs не идемпотентны)
:if ([:len [/ip firewall nat find comment~"hs: block page"]] > 0) do={
  :error "Правила block page уже есть — повторный импорт создал бы дубли. Ничего не сделано."
}

# Заблокированным разрешается доступ ТОЛЬКО к панели (:8000) и к DNS AdGuard
# (:53) — до общего drop'а. DNS обязателен: без резолва имени браузер не дойдёт
# до HTTP-перехвата и человек увидит «нет интернета» вместо страницы. Весь
# остальной трафик заблокированных по-прежнему падает.
/ip firewall filter
add chain=forward action=accept protocol=tcp dst-address=$piAddr dst-port=8000 src-address-list=hs-blocked comment="hs: allow block page" place-before=[find comment="hs: drop blocked devices"]
add chain=forward action=accept protocol=udp dst-address=$piAddr dst-port=53 src-address-list=hs-blocked comment="hs: allow block page dns" place-before=[find comment="hs: drop blocked devices"]
add chain=forward action=accept protocol=tcp dst-address=$piAddr dst-port=53 src-address-list=hs-blocked comment="hs: allow block page dns tcp" place-before=[find comment="hs: drop blocked devices"]

# Метка перехваченного HTTP ДО dst-nat — для hairpin masquerade по метке.
# Правило для hs-unknown ВЫКЛЮЧЕНО, как и его NAT-пара ниже: включать их
# строго ВМЕСТЕ, иначе перехват unknown пойдёт без hairpin и страница
# у них не откроется (выстраданное правило №3).
/ip firewall mangle
add chain=prerouting action=mark-connection new-connection-mark=hs-blockpage protocol=tcp dst-port=80 src-address-list=hs-blocked dst-address=!$piAddr passthrough=yes comment="hs: mark block page"
add chain=prerouting action=mark-connection new-connection-mark=hs-blockpage protocol=tcp dst-port=80 src-address-list=hs-unknown dst-address=!$piAddr passthrough=yes comment="hs: mark block page (unknown)" disabled=yes

# Перехват HTTP -> страница на панели. Исключаем сам Pi и адреса роутера.
/ip firewall nat
add chain=dstnat action=dst-nat protocol=tcp dst-port=80 src-address-list=hs-blocked dst-address=!$piAddr dst-address-type=!local to-addresses=$piAddr to-ports=8000 comment="hs: block page (blocked)"
add chain=dstnat action=dst-nat protocol=tcp dst-port=80 src-address-list=hs-unknown dst-address=!$piAddr dst-address-type=!local to-addresses=$piAddr to-ports=8000 comment="hs: block page (unknown portal)" disabled=yes

# Hairpin: маскируем ТОЛЬКО перехваченные (помеченные) соединения. Прямые
# заходы на панель (dst-port 8000, не 80) не метятся → их настоящий IP
# доходит до панели, и она показывает персональную причину блокировки.
add chain=srcnat action=masquerade connection-mark=hs-blockpage place-before=[find comment="defconf: masquerade"] comment="hs: block page hairpin"

:put "Страница «время вышло» включена. Портал для неизвестных (второе правило) выключен — включайте только вместе с HS_BLOCK_UNKNOWN=true."
