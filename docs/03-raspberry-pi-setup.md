# Шаг 3. Raspberry Pi: AdGuard Home + панель HomeSec

Pi 3B (1 ГБ RAM) спокойно тянет обе службы. Предполагается свежий
Raspberry Pi OS Lite (64-bit), Pi подключён кабелем и имеет адрес 192.168.88.2
(закреплён на шаге 2).

## 1. Установите AdGuard Home

```bash
curl -s -S -L https://raw.githubusercontent.com/AdguardTeam/AdGuardHome/master/scripts/install.sh | sh -s -- -v
```

Откройте `http://192.168.88.2:3000` и пройдите мастер:

- **Web interface**: порт `3000`.
- **DNS server**: порт `53`, слушать на всех интерфейсах.
- Придумайте логин/пароль — они же пойдут в `.env` панели.

Если порт 53 занят системным резолвером:

```bash
sudo systemctl disable --now systemd-resolved 2>/dev/null || true
sudo rm -f /etc/resolv.conf
echo "nameserver 127.0.0.1" | sudo tee /etc/resolv.conf
```

### Рекомендуемые настройки AdGuard

- **Filters → DNS blocklists**: включите AdGuard DNS filter; добавьте
  список для взрослого контента (например, HaGeZi или OISD NSFW).
- **Settings → DNS settings → Upstream**: `https://dns10.quad9.net/dns-query`
  или `1.1.1.3` (Cloudflare Family — сразу режет взрослый контент).
- **Query log**: хранить 7–30 дней (это сырьё для будущих ИИ-отчётов).

Per-client настройки (блокировка YouTube/игр для группы «Дети», безопасный
поиск) руками задавать не нужно — их создаёт и обновляет панель HomeSec.

## 2. Установите панель HomeSec

```bash
# на Pi
git clone <ваш-репозиторий> homesec && cd homesec   # или scp -r проекта
sudo bash deploy/install.sh
```

Заполните `/opt/homesec/backend/.env`:

```ini
HS_ADMIN_USERNAME=admin
HS_ADMIN_PASSWORD=<пароль входа в панель>
HS_SECRET_KEY=<openssl rand -hex 32>
HS_MIKROTIK_HOST=192.168.88.1
HS_MIKROTIK_USER=homesec
HS_MIKROTIK_PASSWORD=<пароль из homesec-base.rsc>
HS_ADGUARD_URL=http://127.0.0.1:3000
HS_ADGUARD_USERNAME=<логин AdGuard>
HS_ADGUARD_PASSWORD=<пароль AdGuard>
HS_BLOCK_UNKNOWN=false
```

```bash
sudo systemctl restart homesec
journalctl -u homesec -f   # убедитесь, что нет ошибок подключения
```

Панель: **http://192.168.88.2:8000**

## 3. Первичная настройка в панели

1. **Пользователи** → добавьте членов семьи (дети / взрослые).
2. **Устройства** → «Сканировать сеть» → назовите устройства и привяжите
   к владельцам. IP закрепляются автоматически.
3. **Правила** → создайте расписание, например «Ночь без интернета»:
   группа «Дети», Пн–Вс, с 22:00 до 07:00.
4. **Политики групп** → для «Детей» отметьте «Игры», «YouTube и видео»
   по необходимости + «Безопасный поиск».
5. Когда все домашние устройства опознаны, включите `HS_BLOCK_UNKNOWN=true` —
   новые неизвестные устройства будут без интернета, пока вы их не одобрите.

## Как это переживает сбои

- **Pi перезагрузился** — systemd поднимет обе службы; панель при старте
  делает reconcile и восстанавливает блокировки.
- **Роутер перезагрузился** — address-list'ы и правила хранятся в его
  конфигурации; панель на ближайшем тике (раз в минуту) досинхронизирует.
- **Pi выключили совсем** — сеть остаётся без DNS (интернет фактически
  «встанет»). Это осознанный fail-closed: выключить Pi ≠ обойти контроль.

## Проверка end-to-end

С детского устройства:

1. `nslookup google.com 8.8.8.8` — ответ должен прийти (подменённый) от
   AdGuard, а в Query log AdGuard появится запись с IP устройства.
2. Заблокируйте устройство в панели — интернет должен пропасть в течение
   пары секунд (активные соединения рвутся принудительно).
3. Попробуйте включить в браузере «Secure DNS» (DoH) — сайты перестанут
   открываться (порт 853 и известные DoH-серверы закрыты), браузер
   откатится на обычный DNS.
