# Шаг 4. CI/CD: тесты в GitHub + авто-деплой в малинку

Малинка сидит за домашним NAT, поэтому GitHub-раннер не может «достучаться»
до неё снаружи. Схема **pull**: тесты гоняются в облаке GitHub, а Pi сам
подтягивает изменения и перезапускается.

```text
git push  ──►  GitHub Actions (CI: pytest)  ──►  ветка main обновлена
                                                        │
Raspberry Pi:  таймер каждую минуту ──► update.sh ──► git pull + restart
```

## CI (уже настроено)

`.github/workflows/ci.yml` на каждый push и pull request:

- ставит зависимости из `backend/requirements.txt`;
- гоняет `pytest` (логика расписаний, расчёт состояния сети, смоук всех
  страниц панели). Реального роутера/AdGuard тестам не нужно — интеграции
  замоканы недоступными портами.

## CD — авто-деплой на Pi

Настраивается один раз при установке (`deploy/install.sh` ставит таймер
`homesec-update.timer`). Дальше каждую минуту `deploy/update.sh`:

1. `git fetch` ветки `main`;
2. если SHA изменился — `git reset --hard`, `pip install -r requirements.txt`,
   `systemctl restart homesec`.

`.env` с паролями лежит вне гита (`.gitignore`) и при обновлении не трогается.

### Ручной деплой / откат

```bash
sudo systemctl start homesec-update      # подтянуть сейчас, не ждать таймер
journalctl -u homesec-update -f          # смотреть, что деплоится
sudo git -C /opt/homesec reset --hard <sha>  # откат на конкретный коммит
```

## Рабочий цикл

1. Правим код локально, `git push`.
2. GitHub гоняет тесты — если красные, чиним (на Pi ничего не уедет, пока
   не смержено в `main`).
3. В течение минуты Pi обновляется сам. Проверяем `journalctl -u homesec`.

Хотите деплоить только проверенное — держите разработку в ветках и вливайте
в `main` только после зелёного CI (можно включить branch protection в
настройках репозитория).
