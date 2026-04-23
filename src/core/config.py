from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    environment: str = "development"
    log_level: str = "info"
    data_dir: Path = Path.home() / ".discord-scanner"

    class Config:
        env_prefix = "APP_"
        env_file = ".env"


settings = Settings()