from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_local_env() -> None:
    """Минимальный загрузчик .env без обязательной runtime-зависимости."""
    env_path = Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _ids(name: str) -> frozenset[int]:
    return frozenset(
        int(value.strip()) for value in os.getenv(name, "").split(",") if value.strip().isdigit()
    )


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    max_bot_token: str = ""
    max_api_base_url: str = "https://platform-api2.max.ru"
    max_polling_timeout: int = 30
    max_request_timeout: int = 40
    database_url: str = "postgresql://max_user:change_me@postgres:5432/max_app"
    uk_operator_ids: frozenset[int] = frozenset()
    uk_access_code: str = ""
    confirmation_timeout_minutes: int = 2880
    max_bot_link: str = ""
    display_utc_offset_hours: int = 3
    uk_admin_ids: frozenset[int] = frozenset()
    platform_admin_ids: frozenset[int] = frozenset()
    org_name: str = "Тестовая УК «Демо»"
    official_appeal_url: str = "https://dom.gosuslugi.ru"
    seed_demo: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        _load_local_env()
        return cls(
            max_bot_token=os.getenv("MAX_BOT_TOKEN", "").strip(),
            max_api_base_url=os.getenv(
                "MAX_API_BASE_URL", "https://platform-api2.max.ru"
            ).rstrip("/"),
            max_polling_timeout=int(os.getenv("MAX_POLLING_TIMEOUT", "30")),
            max_request_timeout=int(os.getenv("MAX_REQUEST_TIMEOUT", "40")),
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql://max_user:change_me@postgres:5432/max_app",
            ),
            uk_operator_ids=_ids("UK_OPERATOR_IDS"),
            uk_access_code=os.getenv("UK_ACCESS_CODE", "").strip(),
            confirmation_timeout_minutes=int(
                os.getenv("CONFIRMATION_TIMEOUT_MINUTES", "2880")
            ),
            max_bot_link=os.getenv("MAX_BOT_LINK", "").strip(),
            display_utc_offset_hours=int(os.getenv("DISPLAY_UTC_OFFSET_HOURS", "3")),
            uk_admin_ids=_ids("UK_ADMIN_IDS"),
            platform_admin_ids=_ids("PLATFORM_ADMIN_IDS"),
            org_name=os.getenv("ORG_NAME", "Тестовая УК «Демо»").strip(),
            official_appeal_url=os.getenv("OFFICIAL_APPEAL_URL", "https://dom.gosuslugi.ru").strip(),
            seed_demo=_flag("SEED_DEMO"),
        )
