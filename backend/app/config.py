from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HS_", env_file=".env", extra="ignore")

    admin_username: str = "admin"
    admin_password: str = "admin"
    secret_key: str = "dev-insecure-key"

    mikrotik_host: str = "192.168.88.1"
    mikrotik_user: str = "homesec"
    mikrotik_password: str = ""

    # Статический якорь «собственных» IP малинки: они никогда не попадают в
    # списки контроля (иначе AdGuard на этом же хосте окажется управляемым и дом
    # останется без DNS). Дополняет автоопределение по маршруту — работает, даже
    # если оно отвалилось. По умолчанию — LAN-адрес Pi. csv для нескольких.
    self_ips: str = "192.168.88.2"

    adguard_url: str = "http://127.0.0.1:3000"
    adguard_username: str = ""
    adguard_password: str = ""

    block_unknown: bool = False
    database_path: str = "./homesec.db"
    scheduler_enabled: bool = True  # выключается для локальной разработки/тестов

    # ИИ-слой (Claude API); пустой ключ = ИИ-фичи выключены, остальное работает
    anthropic_api_key: str = ""
    ai_model: str = "claude-opus-4-8"  # рассуждения: NL-команды, дайджест
    ai_model_fast: str = "claude-haiku-4-5"  # рутина: оформление алертов
    ai_daily_token_budget: int = 300_000  # суммарно (вход+выход) в день; 0 = без лимита

    # Telegram-бот (отдельный процесс homesec-bot); пустой токен = бот выключен
    telegram_bot_token: str = ""
    telegram_chat_ids: str = ""  # csv chat_id: кому разрешены команды и куда слать алерты
    # Опц. доп. защита: csv user_id, кому разрешено КОМАНДОВАТЬ ботом. В личке
    # chat_id == user_id, так что для одиночного родителя не нужно. Задайте,
    # если бот живёт в ГРУППЕ: иначе командовать может любой участник группы.
    telegram_user_ids: str = ""
    panel_url: str = "http://127.0.0.1:8000"  # для health-проверки панели ботом
    # Адрес панели ИЗ домашней сети — сюда редиректим HTTP, перехваченный
    # enable-block-page.rsc: hairpin-masquerade скрывает IP клиента от панели,
    # а прямое соединение приходит с настоящим IP (нужен для /blocked и /register)
    panel_lan_url: str = "http://192.168.88.2:8000"

    @property
    def telegram_allowed_ids(self) -> set[int]:
        return self._parse_ids(self.telegram_chat_ids)

    @property
    def telegram_allowed_user_ids(self) -> set[int]:
        return self._parse_ids(self.telegram_user_ids)

    @staticmethod
    def _parse_ids(raw: str) -> set[int]:
        out = set()
        for part in raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.add(int(part))
        return out


settings = Settings()
