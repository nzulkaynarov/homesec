# ============================================================================
# HomeSec — сторож малинки на роутере (ответ на ЧП 2026-07-27: Pi зависла,
# весь дом остался без DNS, потому что весь DNS завёрнут на неё).
#
# Импорт:  /import file-name=pi-guard.rsc
# Скрипт МОЖНО импортировать повторно: он сначала удаляет свои прежние
# сущности по именам/комментариям (правило №5 про дубли к нему не относится).
#
# Что делает: раз в минуту СПРАШИВАЕТ У МАЛИНКИ DNS-ИМЯ. Три неудачи подряд
# (≈3 минуты) — роутер САМ берёт DNS на себя и шлёт алерт в Telegram; малинка
# ответила — сам возвращает контроль на AdGuard и пишет об этом.
#
# Почему проба именно DNS, а не пинг: в ЧП 2026-07-27 малинка была ЖИВА и
# пинговалась (панель и бот писали в журнал), но AdGuard перестал отвечать —
# и дом сидел без интернета час. Пинг такую аварию не увидел бы вовсе.
# Синтаксис `:resolve ... server=` проверен на этом роутере (RouterOS 7.23.2).
#
# Почему «спасательный круг», а не переключение DHCP: клиенты узнают о новом
# DNS только при продлении аренды (до 10 минут), а тут дом уже сидит без
# интернета. Поэтому DHCP не трогаем — клиенты продолжают слать запросы на
# 192.168.88.2, а роутер в аварии заворачивает эти запросы на себя. Работает
# мгновенно и для всех, включая гостей, и так же мгновенно откатывается.
# Заворот в своей же подсети = обязательный hairpin masquerade по метке
# (выстраданное правило №3) — поэтому правил четыре, а не одно.
#
# В обычной жизни ВСЕ правила ниже ВЫКЛЮЧЕНЫ и ни на что не влияют:
# включает их только сам сторож и только когда DNS малинки не отвечает.
#
# ПЕРЕД ИМПОРТОМ: подставь токен бота и chat_id родителя (иначе алертов не
# будет, переключение всё равно работает). Взять из /opt/homesec/backend/.env
# на малинке: HS_TELEGRAM_BOT_TOKEN и HS_TELEGRAM_CHAT_IDS (первый id).
#
# ВНИМАНИЕ (испытание 2026-08-15): обнаружение, алерт и возврат контроля
# проверены вживую и работают, а вот «спасательный круг» НЕ спасает: пока
# AdGuard лежал, запросы на 192.168.88.2 (а так ходит весь дом — DHCP раздаёт
# именно этот адрес) оставались без ответа. Так что пункт 1) ниже про «интернет
# дома жив, сайты открываются» сейчас НЕ соответствует поведению. Диагностика —
# в CLAUDE.md, раздел «Испытание сторожа».
#
# ПРОВЕРКА после импорта (делать при владельце, а не ночью). Лучше повторять
# ровно ту аварию, что была: остановить AdGuard, не трогая саму малинку —
#   на малинке: sudo systemctl stop AdGuardHome
#   1) через ~3 минуты: /log print where message~"hs:" — должно быть
#      «hs: DNS малинки не отвечает», в Telegram — алерт, интернет дома жив,
#      сайты открываются, реклама больше не режется (контроль снят — так и надо);
#   2) на малинке: sudo systemctl start AdGuardHome — через минуту в журнале
#      «hs: DNS малинки вернулся».
#   Состояние в любой момент: /system script environment print (hsPiFails).
#
# УДАЛИТЬ сторожа: /system scheduler remove [find name="hs-pi-guard"]
#                  /system script remove [find name="hs-pi-guard"]
# ============================================================================

:local piAddr "192.168.88.2"
:local routerAddr "192.168.88.1"

# --- 0. Чистим прежнюю установку (делает импорт повторяемым) ------------------
/system scheduler remove [find name="hs-pi-guard"]
/system script remove [find name="hs-pi-guard"]
/ip firewall mangle remove [find comment~"hs: lifeboat"]
/ip firewall nat remove [find comment~"hs: lifeboat"]
/ip firewall filter remove [find comment~"hs: lifeboat"]

# --- 1. «Спасательный круг»: выключенные правила на случай смерти малинки -----
# Метка для запросов, адресованных мёртвой малинке (её собственный трафик
# отсекается правилом «hs: DNS Pi exempt», которое стоит выше).
/ip firewall mangle
add chain=prerouting action=mark-connection new-connection-mark=hs-lifeboat protocol=udp dst-port=53 dst-address=$piAddr passthrough=yes comment="hs: lifeboat DNS mark udp" disabled=yes
add chain=prerouting action=mark-connection new-connection-mark=hs-lifeboat protocol=tcp dst-port=53 dst-address=$piAddr passthrough=yes comment="hs: lifeboat DNS mark tcp" disabled=yes
# Заворот на сам роутер + hairpin (иначе ответ придёт «не от того», правило №3).
/ip firewall nat
add chain=dstnat action=dst-nat connection-mark=hs-lifeboat to-addresses=$routerAddr comment="hs: lifeboat DNS -> router" disabled=yes
add chain=srcnat action=masquerade connection-mark=hs-lifeboat place-before=[find comment="defconf: masquerade"] comment="hs: lifeboat DNS hairpin" disabled=yes
# Гостям вход на роутер закрыт — на время аварии открываем им только DNS.
/ip firewall filter
add chain=input action=accept protocol=udp dst-port=53 in-interface=bridge-guest comment="hs: lifeboat guest DNS" place-before=[find comment="hs: guest no router access"] disabled=yes

# --- 2. Сам сторож ------------------------------------------------------------
/system script
add name="hs-pi-guard" policy=read,write,test source={
  :local piAddr "192.168.88.2"
  :local publicDns "1.1.1.1,8.8.8.8"
  :local probeName "example.com"
  # Подставь свои значения, иначе алертов не будет (переключение всё равно работает).
  :local tgToken "CHANGE_ME_BOT_TOKEN"
  :local tgChat "CHANGE_ME_CHAT_ID"
  :local failsToSwitch 3

  :global hsPiFails
  :if ([:typeof $hsPiFails] = "nothing") do={ :set hsPiFails 0 }

  # Текущее состояние определяем по самому правилу заворота, а не по памяти:
  # после перезагрузки роутера сторож всё равно знает, где он находится.
  :local natIds [/ip firewall nat find comment="hs: force DNS -> AdGuard"]
  :if ([:len $natIds] = 0) do={
    :log error "hs: сторож не нашёл правило заворота DNS — homesec-base.rsc не импортирован?"
    :error "no hs nat rule"
  }
  :local controlOn true
  :if ([/ip firewall nat get [:pick $natIds 0] disabled]) do={ :set controlOn false }

  # Проба: спрашиваем у малинки настоящее имя. Отвечает (в том числе 0.0.0.0
  # для заблокированного домена) — значит DNS дома жив, а это и есть услуга,
  # ради которой всё построено. Молчит или ошибка — считаем аварией.
  :local alive false
  :do {
    :resolve $probeName server=$piAddr
    :set alive true
  } on-error={ :set alive false }
  :if ($alive) do={ :set hsPiFails 0 } else={ :set hsPiFails ($hsPiFails + 1) }

  # ---- малинка умерла: роутер берёт DNS на себя ----
  :if ((!$alive) && ($hsPiFails >= $failsToSwitch) && $controlOn) do={
    /ip dns set servers=$publicDns
    /ip dns cache flush
    /ip firewall nat disable [find comment="hs: force DNS -> AdGuard"]
    /ip firewall nat disable [find comment="hs: hairpin DNS"]
    /ip firewall mangle enable [find comment~"hs: lifeboat"]
    /ip firewall nat enable [find comment~"hs: lifeboat"]
    /ip firewall filter enable [find comment~"hs: lifeboat"]
    :log warning "hs: DNS малинки не отвечает — роутер взял DNS на себя, контроль снят"
    :if ($tgToken != "CHANGE_ME_BOT_TOKEN") do={
      :do {
        /tool fetch mode=https check-certificate=no output=none http-method=post  url="https://api.telegram.org/bot$tgToken/sendMessage"  http-data="chat_id=$tgChat&text=%E2%9A%A0%EF%B8%8F%20HomeSec%3A%20%D0%BC%D0%B0%D0%BB%D0%B8%D0%BD%D0%BA%D0%B0%20%D0%BD%D0%B5%20%D0%BE%D1%82%D0%B2%D0%B5%D1%87%D0%B0%D0%B5%D1%82%20%D0%BD%D0%B0%20DNS%20%D1%83%D0%B6%D0%B5%203%20%D0%BC%D0%B8%D0%BD%D1%83%D1%82%D1%8B.%20%D0%A0%D0%BE%D1%83%D1%82%D0%B5%D1%80%20%D0%B2%D0%B7%D1%8F%D0%BB%20DNS%20%D0%BD%D0%B0%20%D1%81%D0%B5%D0%B1%D1%8F%20%E2%80%94%20%D0%B8%D0%BD%D1%82%D0%B5%D1%80%D0%BD%D0%B5%D1%82%20%D0%B4%D0%BE%D0%BC%D0%B0%20%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D0%B5%D1%82%2C%20%D0%BD%D0%BE%20%D1%80%D0%BE%D0%B4%D0%B8%D1%82%D0%B5%D0%BB%D1%8C%D1%81%D0%BA%D0%B8%D0%B9%20%D0%BA%D0%BE%D0%BD%D1%82%D1%80%D0%BE%D0%BB%D1%8C%20%D1%81%D0%B5%D0%B9%D1%87%D0%B0%D1%81%20%D0%9D%D0%95%20%D0%B4%D0%B5%D0%B9%D1%81%D1%82%D0%B2%D1%83%D0%B5%D1%82.%20%D0%9F%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D1%8C%20AdGuard%20%D0%BD%D0%B0%20%D0%BC%D0%B0%D0%BB%D0%B8%D0%BD%D0%BA%D0%B5."
      } on-error={ :log error "hs: алерт в Telegram не ушёл" }
    }
  }

  # ---- малинка вернулась: отдаём DNS обратно AdGuard ----
  :if ($alive && (!$controlOn)) do={
    /ip firewall mangle disable [find comment~"hs: lifeboat"]
    /ip firewall nat disable [find comment~"hs: lifeboat"]
    /ip firewall filter disable [find comment~"hs: lifeboat"]
    /ip firewall nat enable [find comment="hs: force DNS -> AdGuard"]
    /ip firewall nat enable [find comment="hs: hairpin DNS"]
    /ip dns set servers=$piAddr
    /ip dns cache flush
    :log warning "hs: DNS малинки вернулся — DNS снова через AdGuard, контроль восстановлен"
    :if ($tgToken != "CHANGE_ME_BOT_TOKEN") do={
      :do {
        /tool fetch mode=https check-certificate=no output=none http-method=post  url="https://api.telegram.org/bot$tgToken/sendMessage"  http-data="chat_id=$tgChat&text=%E2%9C%85%20HomeSec%3A%20%D0%BC%D0%B0%D0%BB%D0%B8%D0%BD%D0%BA%D0%B0%20%D1%81%D0%BD%D0%BE%D0%B2%D0%B0%20%D0%BE%D1%82%D0%B2%D0%B5%D1%87%D0%B0%D0%B5%D1%82.%20DNS%20%D0%B2%D0%BE%D0%B7%D0%B2%D1%80%D0%B0%D1%89%D1%91%D0%BD%20%D0%BD%D0%B0%20AdGuard%2C%20%D1%80%D0%BE%D0%B4%D0%B8%D1%82%D0%B5%D0%BB%D1%8C%D1%81%D0%BA%D0%B8%D0%B9%20%D0%BA%D0%BE%D0%BD%D1%82%D1%80%D0%BE%D0%BB%D1%8C%20%D0%B2%D0%BE%D1%81%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BB%D0%B5%D0%BD."
      } on-error={ :log error "hs: алерт в Telegram не ушёл" }
    }
  }
}

# --- 3. Расписание ------------------------------------------------------------
/system scheduler
add name="hs-pi-guard" interval=1m on-event="/system script run hs-pi-guard" policy=read,write,test,policy comment="hs: сторож малинки"

:put "Сторож малинки установлен: DNS-проба раз в минуту, переключение после 3 неудач."
:put "НЕ ЗАБУДЬ подставить токен бота и chat_id: /system script edit hs-pi-guard source"
:put "Проверить вживую: выключить малинку и через 3 минуты посмотреть /log print where message~\"hs:\""
