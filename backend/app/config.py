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


settings = Settings()
