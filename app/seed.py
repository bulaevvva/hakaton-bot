"""Демо-данные: УК, справочник домов, сотрудники, жители и заявки во всех статусах.

Запуск против базы из DATABASE_URL:
    python -m app.seed --resident-id <ваш MAX user_id> --admin-id <id сотрудника УК>
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from .domain import (
    Announcement,
    Directory,
    House,
    Issue,
    IssueEvent,
    IssueStatus,
    Repository,
    Resident,
    Role,
    end_of_local_day,
    utcnow,
)

DEMO_RESIDENT_ID = 1
DEMO_OPERATOR_ID = 2
DEMO_MASTER_ID = 3
DEFAULT_ORG_ID = "default"

HOUSES = [
    # id, адрес, подъездов, широта, долгота
    ("len10", "ул. Ленина, 10", 4, 55.75120, 37.61840),
    ("len12", "ул. Ленина, 12", 3, 55.75190, 37.61970),
    ("mira5", "пр-т Мира, 5", 3, 55.78010, 37.63250),
    ("sad3", "ул. Садовая, 3к1", 2, 55.76500, 37.60900),
]

S = IssueStatus
# id, категория, дом, подъезд, описание, статус, возраст ч, жителей,
# с демо-жителем, взята через ч, выполнена через ч, подтверждена через ч
DEMO_ISSUES = [
    ("demo01", "протечка", "len10", "1", "Течёт труба в подвале, вода у стены", S.IN_PROGRESS, 30, 4, True, 3, None, None),
    ("demo02", "свет", "len10", "1", "Не горит лампа у почтовых ящиков", S.WAITING_CONFIRMATION, 30, 3, True, 5, 26, None),
    ("demo03", "двор", "len10", "1", "Не открывается шлагбаум на въезде во двор", S.CONFIRMED, 100, 9, True, 2, 20, 30),
    ("demo04", "отопление", "len12", "1", "Холодные батареи во всём доме", S.OPEN, 6, 5, False, None, None, None),
    ("demo05", "уборка", "len12", "2", "Не моют подъезд вторую неделю", S.OPEN, 30, 2, False, None, None, None),
    ("demo06", "лифт", "mira5", "1", "Лифт не работает с утра", S.OPEN, 26, 7, False, None, None, None),
    ("demo07", "свет", "mira5", "3", "Мигает свет на 7 этаже", S.IN_PROGRESS, 10, 1, False, 2, None, None),
    ("demo08", "протечка", "mira5", "2", "Протечка с крыши после дождя", S.DISPUTED, 60, 3, False, 4, 30, None),
    ("demo09", "отопление", "sad3", "1", "Нет горячей воды", S.CONFIRMED, 150, 4, False, 1, 8, 20),
    ("demo10", "свет", "sad3", "2", "Не работает освещение у входа", S.COMPLETED_NO_RESPONSE, 200, 2, False, 10, 40, None),
    ("demo11", "другое", "sad3", "1", "Сломан кодовый замок на двери", S.IN_PROGRESS, 18, 1, False, 3, None, None),
    ("demo12", "мусоропровод", "len10", "2", "Засор мусоропровода", S.CONFIRMED, 80, 2, False, 6, 30, 40),
    ("demo13", "свет", "sad3", "2", "Снова не горит свет у входа", S.OPEN, 5, 1, False, None, None, None),
]


def seed_demo(
    repository: Repository,
    resident_id: int | None = DEMO_RESIDENT_ID,
    admin_id: int | None = DEMO_OPERATOR_ID,
    master_id: int | None = DEMO_MASTER_ID,
    *,
    org_name: str = "Тестовая УК «Демо»",
    now: datetime | None = None,
) -> int:
    """Добавляет демо-данные; повторный запуск ничего не дублирует.

    Вымышленные соседи участвуют в заявках, но без согласия на рассылки — им
    не уходят оповещения и объявления. resident_id=None — без реального жителя.
    """
    now = now or utcnow()
    directory = Directory(repository)
    org = directory.ensure_org(org_name, DEFAULT_ORG_ID)
    houses = {}
    for house_id, address, entrances, lat, lon in HOUSES:
        house = directory.get_house(house_id) or repository.put(House(
            id=house_id, org_id=org.id, address=address, entrances=entrances,
            latitude=lat, longitude=lon,
        ))
        houses[house_id] = house
    if admin_id is not None and directory.staff(admin_id) is None:
        directory.add_staff(admin_id, org.id, Role.ADMIN, "Диспетчер Ольга")
    if master_id is not None and directory.staff(master_id) is None:
        directory.add_staff(master_id, org.id, Role.MASTER, "Сантехник Иван")

    created = 0
    neighbor = 900_000
    for (issue_id, category, house_id, entrance, description, status, age, residents,
         with_resident, taken_after, completed_after, confirmed_after) in DEMO_ISSUES:
        if repository.get(issue_id) is not None:
            continue
        house = houses[house_id]
        created_at = now - timedelta(hours=age)
        participants: list[int] = [resident_id] if with_resident and resident_id is not None else []
        while len(participants) < residents:
            neighbor += 1
            participants.append(neighbor)
            repository.put(Resident(user_id=neighbor, house_id=house.id, entrance=entrance))
        at = lambda hours: created_at + timedelta(hours=hours)  # noqa: E731
        issue = Issue(
            id=issue_id, house=house.address, house_id=house.id, house_key=house.house_key,
            org_id=org.id, entrance=entrance, category=category, description=description,
            author_id=participants[0], status=status, participants=set(participants),
            created_at=created_at,
            taken_at=at(taken_after) if taken_after is not None else None,
            completed_at=at(completed_after) if completed_after is not None else None,
            confirmed_at=at(confirmed_after) if confirmed_after is not None else None,
        )
        events = [IssueEvent(issue_id, "created", participants[0], created_at=created_at)]
        for index, user in enumerate(participants[1:], start=1):
            via = "alert" if index % 2 else "match"
            events.append(IssueEvent(issue_id, "joined", user, {"via": via}, created_at=at(0.5 * index)))
        if taken_after is not None:
            events.append(IssueEvent(issue_id, "taken", admin_id, created_at=at(taken_after)))
        if issue_id == "demo01":
            issue.planned_at = end_of_local_day(1, now)
            issue.uk_comment = "Сантехник придёт завтра с 10 до 12, перекроем стояк на час"
            issue.assignee_id = master_id
        if completed_after is not None:
            events.append(IssueEvent(issue_id, "completed", admin_id, created_at=at(completed_after)))
        if status is S.WAITING_CONFIRMATION:
            issue.confirmations = {participants[-1]}
        if status is S.CONFIRMED:
            issue.confirmations = set(participants[: len(participants) // 2 + 1])
            events.append(IssueEvent(issue_id, "confirmed", None, created_at=at(confirmed_after)))
        if status is S.DISPUTED:
            issue.dispute_count = 1
            issue.completed_at = None
            events.append(IssueEvent(issue_id, "disputed", participants[0], {"round": 1}, created_at=at(completed_after + 5)))
        if status is S.COMPLETED_NO_RESPONSE:
            events.append(IssueEvent(issue_id, "no_response", None, created_at=at(completed_after + 48)))
        issue.updated_at = max(event.created_at for event in events)
        repository.add(issue)
        for event in events:
            repository.put(event)
        created += 1

    if created and resident_id is not None:
        repository.put(Resident(
            user_id=resident_id, house_id="len10", entrance="1", consent_at=now - timedelta(days=5),
        ))
    if created:
        repository.put(Announcement(
            house_id="len10",
            text="Завтра с 10:00 до 14:00 плановое отключение горячей воды. Просим заранее набрать воду.",
            author_id=admin_id or 0,
            recipients=12,
            created_at=now - timedelta(hours=3),
        ))
    return created


def main() -> None:
    from .config import Settings
    from .store import PostgresIssueRepository, SqliteIssueRepository

    parser = argparse.ArgumentParser(description="Заполнить базу демо-данными")
    parser.add_argument("--resident-id", type=int, default=DEMO_RESIDENT_ID,
                        help="MAX user_id жителя дома «ул. Ленина, 10», подъезд 1")
    parser.add_argument("--admin-id", type=int, default=None,
                        help="MAX user_id, который станет администратором демо-УК")
    parser.add_argument("--master-id", type=int, default=None,
                        help="MAX user_id исполнителя (сантехника) демо-УК")
    parser.add_argument("--sqlite", help="путь к SQLite вместо PostgreSQL из DATABASE_URL")
    args = parser.parse_args()

    settings = Settings.from_env()
    repository = (
        SqliteIssueRepository(args.sqlite)
        if args.sqlite
        else PostgresIssueRepository(settings.database_url)
    )
    try:
        count = seed_demo(
            repository, resident_id=args.resident_id, admin_id=args.admin_id,
            master_id=args.master_id, org_name=settings.org_name,
        )
    finally:
        repository.close()
    print(f"Добавлено демо-заявок: {count}")


if __name__ == "__main__":
    main()
