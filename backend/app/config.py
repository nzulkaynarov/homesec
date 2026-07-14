from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HS_", env_file=".env", extra="ignore")

    admin_username: str = "admin"
    admin_password: str = "admin"
    secret_key: str = "dev-insecure-key"

    mikrotik_host: str = "192.168.88.1"
    mikrotik_user: str = "homesec"
    mikrotik_password: str = ""

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
    panel_url: str = "http://127.0.0.1:8000"  # для health-проверки панели ботом
    # Адрес панели ИЗ домашней сети — сюда редиректим HTTP, перехваченный
    # enable-block-page.rsc: hairpin-masquerade скрывает IP клиента от панели,
    # а прямое соединение приходит с настоящим IP (нужен для /blocked и /register)
    panel_lan_url: str = "http://192.168.88.2:8000"

    @property
    def telegram_allowed_ids(self) -> set[int]:
        out = set()
        for part in self.telegram_chat_ids.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.add(int(part))
        return out


settings = Settings()
