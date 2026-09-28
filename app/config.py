from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # PostgreSQL
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "postgres"
    postgres_password: str = ""
    postgres_database: str = "polybot"

    # Polymarket
    polymarket_wallet: str = ""
    polymarket_private_key: str = ""  # required for CLOB collateral balance fetch
    polymarket_signature_type: int = 1  # 0=eoa, 1=proxy (Magic/email wallets), 2=gnosis-safe

    # Polling
    poll_interval_seconds: int = 60

    # Logging
    log_level: str = "INFO"

    # Authentication
    auth_username: str = "admin"
    auth_password_hash: str = ""  # bcrypt hash of password
    jwt_secret_key: str = "change-me-in-production"  # Used for signing JWT tokens

    # Alerts
    alerts_enabled: bool = True
    ntfy_topic: str = ""  # e.g. "polymarket-alerts-xyz" — leave empty to disable notifications
    ntfy_server: str = "https://ntfy.sh"
    starting_capital: float = 231.00  # Jan 1, 2026 (fills-derived; was 229.13) — used for portfolio drawdown calc
    dashboard_url: str = "https://polymarket.ebertx.com"  # used for ntfy action buttons

    # Docs viewer (/docs) — serves polymarket-team markdown from GitHub
    github_docs_token: str = ""  # fine-grained read-only PAT for ebertx/polymarket-team
    docs_link_secret: str = ""   # HMAC secret shared with Kryten's doc_link.py

    @property
    def database_url(self) -> str:
        from urllib.parse import quote_plus
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{quote_plus(self.postgres_password)}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
