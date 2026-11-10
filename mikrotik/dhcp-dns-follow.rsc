# ============================================================================
# HomeSec — DHCP раздаёт тот DNS, который сейчас работает (2026-11-10).
#
# Импорт:  /import file-name=dhcp-dns-follow.rsc   (повторный импорт безопасен)
#
# Зачем. Раньше клиенты получали два DNS: малинку и роутер запасным. Системы
# (systemd-resolved, macOS, Android) переходят на запасной при первой заминке
# и остаются на нём. Запрос к роутеру заворачивается на AdGuard с маскарадом,
# и AdGuard видит роутер вместо устройства: детские правила по человеку и
# учёт минут на такие запросы не действовали (28% всех запросов в ноябре).
#
# Теперь клиенты получают только малинку. Раз в минуту этот скрипт смотрит,
# включил ли сторож (pi-guard.rsc) аварийный режим, и тогда раздаёт роутер,
# а после восстановления снова малинку. Аренда сокращена до 4 минут, клиенты
# продлевают её через 2, так что при отказе малинки дом остаётся без DNS
# примерно на 5 минут (3 минуты сторож ждёт, потом продление аренды).
# Перехватить запросы к 192.168.88.2 в своей подсети роутер не может: мост
# коммутирует их на L2 (use-ip-firewall=no, правило №9), поэтому только DHCP.
#
# Скрипт сторожа с токеном бота этот файл не трогает.
# ============================================================================

/system scheduler remove [find name=hs-dhcp-dns]
/system script remove [find name=hs-dhcp-dns]

/system script add name=hs-dhcp-dns policy=read,write,test source={
  :local piAddr "192.168.88.2"
  :local routerAddr "192.168.88.1"
  :local lifeboat [/ip firewall nat find comment="hs: lifeboat DNS -> router"]
  :local nets [/ip dhcp-server network find comment=defconf]
  :if (([:len $lifeboat] > 0) && ([:len $nets] > 0)) do={
    :local want $piAddr
    :if (![/ip firewall nat get [:pick $lifeboat 0] disabled]) do={ :set want $routerAddr }
    :local have [:tostr [/ip dhcp-server network get [:pick $nets 0] dns-server]]
    :if ($have != $want) do={
      /ip dhcp-server network set [:pick $nets 0] dns-server=$want
      :log warning "hs-dhcp-dns: DHCP DNS -> $want"
    }
  }
}

/system scheduler add name=hs-dhcp-dns interval=1m on-event=hs-dhcp-dns policy=read,write,test comment="hs: DHCP hands out the working DNS"
/ip dhcp-server set [find name=defconf] lease-time=4m
/system script run hs-dhcp-dns
