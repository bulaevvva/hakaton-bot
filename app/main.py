from __future__ import annotations

import asyncio
import logging

from .bot import BotApplication
from .config import Settings
from .domain import IssueService, set_display_offset
from .max_client import MaxBotClient
from .store import PostgresIssueRepository

logger = logging.getLogger(__name__)


def bot_options(settings: Settings) -> dict:
    return {
        "uk_operator_ids": settings.uk_operator_ids,
        "uk_access_code": settings.uk_access_code,
        "confirmation_timeout_minutes": settings.confirmation_timeout_minutes,
        "uk_admin_ids": settings.uk_admin_ids,
        "platform_admin_ids": settings.platform_admin_ids,
        "bot_link": settings.max_bot_link,
        "org_name": settings.org_name,
        "official_appeal_url": settings.official_appeal_url,
    }


async def run_bot(settings: Settings) -> None:
    client = MaxBotClient(
        token=settings.max_bot_token,
        base_url=settings.max_api_base_url,
        timeout=settings.max_request_timeout,
    )
    repository = None
    try:
        repository = PostgresIssueRepository(settings.database_url)
        if settings.seed_demo:
            from .seed import seed_demo

            # Тестовые данные для проверки: УК, 4 дома, заявки во всех статусах.
            # Сотрудников не создаём — проверяющий входит в роль УК через /uk <код>.
            created = seed_demo(
                repository, resident_id=None, admin_id=None, org_name=settings.org_name
            )
            logger.info("SEED_DEMO: добавлено демо-заявок: %s", created)
        application = BotApplication(client, IssueService(repository), **bot_options(settings))
        await application.run(settings.max_polling_timeout)
    finally:
        if repository is not None:
            repository.close()
        await client.aclose()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    set_display_offset(settings.display_utc_offset_hours)
    if not settings.max_bot_token:
        raise SystemExit(
            "MAX_BOT_TOKEN не задан. Скопируйте .env.example в .env и добавьте токен бота MAX."
        )
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
