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

# Заблокированным разрешается доступ ТОЛЬКО к панели (порт 8000) — до
# общего drop'а. Всё остальное для них по-прежнему падает.
/ip firewall filter
add chain=forward action=accept protocol=tcp dst-address=$piAddr dst-port=8000 src-address-list=hs-blocked comment="hs: allow block page" place-before=[find comment="hs: drop blocked devices"]

# Перехват HTTP -> страница на панели. Исключаем сам Pi и адреса роутера.
/ip firewall nat
add chain=dstnat action=dst-nat protocol=tcp dst-port=80 src-address-list=hs-blocked dst-address=!$piAddr dst-address-type=!local to-addresses=$piAddr to-ports=8000 comment="hs: block page (blocked)"
add chain=dstnat action=dst-nat protocol=tcp dst-port=80 src-address-list=hs-unknown dst-address=!$piAddr dst-address-type=!local to-addresses=$piAddr to-ports=8000 comment="hs: block page (unknown portal)" disabled=yes

:put "Страница «время вышло» включена. Портал для неизвестных (второе правило) выключен — включайте только вместе с HS_BLOCK_UNKNOWN=true."
