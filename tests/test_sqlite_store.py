import sqlite3
from datetime import datetime, timezone

from app.domain import (
    Announcement,
    IssueService,
    IssueStatus,
    Role,
)
from app.store import SqliteIssueRepository


def test_sqlite_persists_directory_issues_events_and_drafts(tmp_path) -> None:
    path = str(tmp_path / "issues.db")
    repository = SqliteIssueRepository(path)
    service = IssueService(repository)
    org = service.directory.ensure_org("УК Тест", "default")
    house = service.directory.add_house(org.id, "ул. Ленина, 10", 4, 55.75, 37.61)
    service.directory.add_staff(5, org.id, Role.DISPATCHER, "Ольга")
    service.directory.set_resident_house(7, house.id, "2")
    issue = service.create(
        house=house, entrance="2", category="лифт", description="Стоит",
        author_id=7, photos=[{"token": "t1"}],
    )
    service.join(issue.id, 8)
    service.take_in_work(issue.id, 5)
    service.mark_completed_by_uk(issue.id, 5, result_photos=[{"token": "r1"}])
    service.vote(issue.id, 7, False)
    repository.put(Announcement(house_id=house.id, text="Вода", author_id=5))
    repository.save_draft(7, {"step": "res_desc", "photos": [{"token": "x"}]})
    repository.close()

    reopened = SqliteIssueRepository(path)
    service = IssueService(reopened)
    loaded = reopened.get(issue.id)

    assert service.directory.get_house(house.id).latitude == 55.75
    assert service.directory.staff(5).role is Role.DISPATCHER
    assert service.directory.resident(7).entrance == "2"
    assert loaded.photos == [{"token": "t1"}] and loaded.result_photos == [{"token": "r1"}]
    assert loaded.participants == {7, 8} and loaded.disputes == {7}
    assert loaded.status is IssueStatus.WAITING_CONFIRMATION
    assert [event.kind for event in service.events(issue.id)] == ["created", "joined", "taken", "completed", "vote"]
    assert service.announcements(house.id)[0].text == "Вода"
    assert reopened.get_draft(7)["photos"] == [{"token": "x"}]
    assert [item.id for item in reopened.list_for_user(8)] == [issue.id]
    reopened.close()


def test_sqlite_migrates_legacy_database(tmp_path) -> None:
    path = str(tmp_path / "legacy.db")
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE issues (
            id TEXT PRIMARY KEY, house TEXT NOT NULL, entrance TEXT NOT NULL,
            category TEXT NOT NULL, description TEXT NOT NULL,
            author_id INTEGER NOT NULL, status TEXT NOT NULL,
            participants TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "INSERT INTO issues VALUES ('old1', 'ул. ленина, 10', '1', 'свет', 'Темно', 1, 'open', '[1]', ?, ?)",
        (now, now),
    )
    connection.commit()
    connection.close()

    repository = SqliteIssueRepository(path)
    legacy = repository.get("old1")
    repository.close()

    assert legacy.house_key == "ленина 10"
    assert legacy.participants == {1} and legacy.disputes == set()
    assert legacy.dispute_count == 0 and legacy.photos == []
