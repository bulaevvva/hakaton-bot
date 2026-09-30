from datetime import timedelta

import pytest

from app.domain import (
    Directory,
    IssueService,
    IssueStatus,
    Role,
    VoteResult,
    normalize_address,
    normalize_entrance,
    priority_score,
)
from app.store import InMemoryIssueRepository


@pytest.fixture
def service() -> IssueService:
    return IssueService(InMemoryIssueRepository())


@pytest.fixture
def house(service: IssueService):
    org = service.directory.ensure_org("УК Тест", "default")
    return service.directory.add_house(org.id, "ул. Ленина, 10", 4, 55.7512, 37.6184)


def new_issue(service, house, category="лифт", entrance="1", author=1):
    return service.create(
        house=house, entrance=entrance, category=category, description="Не работает", author_id=author,
    )


def resident(service, user_id, house, entrance="1"):
    return service.directory.set_resident_house(user_id, house.id, entrance)


# --- адреса и справочник ---

def test_address_spellings_share_one_key() -> None:
    variants = [
        "ул. Ленина, 10", "Ленина 10", "улица Ленина, дом 10", "УЛ.ЛЕНИНА Д.10",
        "г. Казань, ул. Ленина, д. 10", "Ленина ул., 10",
    ]
    assert {normalize_address(value) for value in variants} == {"ленина 10"}


def test_address_keeps_building_and_street_type_differences() -> None:
    assert normalize_address("Ленина 10 корп. 2") == normalize_address("ленина 10к2")
    assert normalize_address("Ленина 10к1") != normalize_address("Ленина 10к2")
    assert normalize_address("пр-т Мира 5") == normalize_address("Мира проспект, 5")
    assert normalize_address("пер. Мира 5") != normalize_address("пр-т Мира 5")


def test_entrance_spellings_are_normalized() -> None:
    assert {normalize_entrance(value) for value in ["1", "1-й", "подъезд 1", "п. 1"]} == {"1"}


def test_directory_deduplicates_houses_and_searches(service, house) -> None:
    directory = service.directory
    assert directory.add_house(house.org_id, "Ленина д. 10", 2).id == house.id
    directory.add_house(house.org_id, "ул. Ленина, 12", 3)
    directory.add_house(house.org_id, "пр-т Мира, 5", 3)

    assert [item.address for item in directory.search_houses("ленина 10")] == ["ул. Ленина, 10"]
    assert {item.address for item in directory.search_houses("ленина")} == {"ул. Ленина, 10", "ул. Ленина, 12"}
    assert directory.search_houses("садовая") == []


def test_nearest_houses_by_location(service, house) -> None:
    far = service.directory.add_house(house.org_id, "пр-т Мира, 5", 3, 55.7801, 37.6325)
    nearest = service.directory.nearest_houses(55.7513, 37.6186)

    assert nearest[0][0].id == house.id and nearest[0][1] < 50
    assert far.id not in {item.id for item, _ in nearest}


# --- объединение по охвату категории ---

def test_entrance_category_matches_only_same_entrance(service, house) -> None:
    lift = new_issue(service, house, "лифт", "1")
    assert service.find_similar(house.id, "подъезд 1", "лифт").id == lift.id
    assert service.find_similar(house.id, "2", "лифт") is None


def test_house_category_matches_across_entrances(service, house) -> None:
    heating = new_issue(service, house, "отопление", "1")
    assert service.find_similar(house.id, "3", "отопление").id == heating.id


def test_other_category_is_never_merged(service, house) -> None:
    new_issue(service, house, "другое")
    assert service.find_similar(house.id, "1", "другое") is None


def test_old_and_closed_issues_are_not_reused(service, house) -> None:
    old = new_issue(service, house, "свет")
    old.created_at -= timedelta(hours=73)
    closed = new_issue(service, house, "мусоропровод")
    service.set_status(closed.id, IssueStatus.CONFIRMED)

    assert service.find_similar(house.id, "1", "свет") is None
    assert service.find_similar(house.id, "1", "мусоропровод") is None


# --- подтверждение большинством ---

def waiting_issue(service, house, participants=3, category="лифт"):
    issue = new_issue(service, house, category)
    for user_id in range(2, participants + 1):
        service.join(issue.id, user_id)
    service.mark_completed_by_uk(issue.id, 99)
    return issue


def test_majority_of_participants_confirms_early(service, house) -> None:
    issue = waiting_issue(service, house, participants=5)

    assert service.vote(issue.id, 1, True)[1] == VoteResult.RECORDED
    assert service.vote(issue.id, 2, True)[1] == VoteResult.RECORDED
    updated, result = service.vote(issue.id, 3, True)

    assert result == VoteResult.CONFIRMED
    assert updated.status is IssueStatus.CONFIRMED and updated.confirmed_at is not None


def test_single_resident_decides_own_issue(service, house) -> None:
    issue = waiting_issue(service, house, participants=1)
    assert service.vote(issue.id, 1, True)[1] == VoteResult.CONFIRMED


def test_tie_after_everyone_answered_returns_issue_to_uk(service, house) -> None:
    issue = waiting_issue(service, house, participants=2)
    service.vote(issue.id, 1, True)
    updated, result = service.vote(issue.id, 2, False)

    assert result == VoteResult.DISPUTED
    assert updated.dispute_count == 1 and updated.completed_at is None


def test_resident_can_change_vote(service, house) -> None:
    issue = waiting_issue(service, house, participants=3)
    service.vote(issue.id, 1, False)
    service.vote(issue.id, 1, True)
    stored = service.repository.get(issue.id)

    assert stored.confirmations == {1} and stored.disputes == set()


def test_chairman_vote_is_decisive(service, house) -> None:
    house.chairman_id = 77
    service.repository.put(house)
    issue = waiting_issue(service, house, participants=5)
    service.vote(issue.id, 1, False)

    updated, result = service.vote(issue.id, 77, True)

    assert result == VoteResult.CONFIRMED and updated.status is IssueStatus.CONFIRMED


def test_only_participants_and_chairman_vote(service, house) -> None:
    issue = waiting_issue(service, house, participants=2)
    assert service.vote(issue.id, 50, True) == (None, None)


def test_vote_is_available_only_after_completion(service, house) -> None:
    issue = new_issue(service, house)
    assert service.vote(issue.id, 1, True) == (None, None)


def test_timeout_uses_majority_of_responders(service, house) -> None:
    issue = waiting_issue(service, house, participants=5)
    service.vote(issue.id, 1, True)
    issue.completed_at -= timedelta(hours=49)

    closed = service.expire_waiting(timedelta(hours=48))

    assert closed[0].status is IssueStatus.CONFIRMED


def test_timeout_without_answers_is_not_confirmation(service, house) -> None:
    issue = waiting_issue(service, house, participants=3)
    issue.completed_at -= timedelta(hours=49)

    closed = service.expire_waiting(timedelta(hours=48))

    assert closed[0].status is IssueStatus.COMPLETED_NO_RESPONSE
    assert closed[0].confirmations == set()


def test_reminder_goes_once_to_silent_participants_at_half_time(service, house) -> None:
    issue = waiting_issue(service, house, participants=3)
    service.vote(issue.id, 1, True)
    assert service.due_reminders(timedelta(hours=48)) == []

    issue.completed_at -= timedelta(hours=25)
    due = service.due_reminders(timedelta(hours=48))

    assert [(item.id, silent) for item, silent in due] == [(issue.id, [2, 3])]
    assert service.due_reminders(timedelta(hours=48)) == []


# --- массовые оповещения ---

def test_mass_alert_scope_follows_category(service, house) -> None:
    for user_id, entrance in [(2, "1"), (3, "1"), (4, "2")]:
        resident(service, user_id, house, entrance)
    lift = new_issue(service, house, "лифт", "1", author=2)
    heating = new_issue(service, house, "отопление", "1", author=2)
    other = new_issue(service, house, "другое", "1", author=2)

    assert service.mass_notify_recipients(lift) == [3]
    assert sorted(service.mass_notify_recipients(heating)) == [3, 4]
    assert service.mass_notify_recipients(other) == []


def test_mass_alert_respects_mute_and_daily_limit(service, house) -> None:
    resident(service, 3, house)
    muted = resident(service, 4, house)
    muted.notify_mass = False
    service.directory.save_resident(muted)

    sent = [service.mass_notify_recipients(new_issue(service, house, "отопление", author=2)) for _ in range(4)]

    assert sent == [[3], [3], [3], []]


# --- УК, приглашения, роли ---

def test_staff_invite_is_single_use(service, house) -> None:
    directory: Directory = service.directory
    invite = directory.create_invite(house.org_id, Role.DISPATCHER.value, 99)

    assert directory.accept_invite(invite.code, 10) is not None
    assert directory.staff(10).role is Role.DISPATCHER
    assert directory.accept_invite(invite.code, 11) is None


def test_chairman_invite_assigns_house(service, house) -> None:
    invite = service.directory.create_invite(house.org_id, "chairman", 99, house.id)
    service.directory.accept_invite(invite.code, 20)

    assert service.directory.is_chairman(20, house.id)
    assert service.directory.resident(20).house_id == house.id


def test_plan_comment_and_assignment_are_logged(service, house) -> None:
    issue = new_issue(service, house)
    service.take_in_work(issue.id, 99)
    service.set_plan(issue.id, 99, comment="Мастер придёт завтра")
    service.assign(issue.id, 55, 99)

    stored = service.repository.get(issue.id)
    assert stored.uk_comment == "Мастер придёт завтра" and stored.assignee_id == 55
    assert [event.kind for event in service.events(issue.id)] == ["created", "taken", "planned", "assigned"]
    assert [item.id for item in service.assigned_to(55)] == [issue.id]


# --- приоритет, сроки, метрики ---

def test_priority_grows_with_neighbors_and_urgency(service, house) -> None:
    light = new_issue(service, house, "свет")
    leak = new_issue(service, house, "протечка")
    assert priority_score(leak) > priority_score(light)

    for user_id in range(2, 8):
        service.join(light.id, user_id)
    assert priority_score(light) > priority_score(leak)
    assert service.queue(house.org_id)[0].id == light.id


def test_overdue_is_reported_once(service, house) -> None:
    issue = new_issue(service, house, "протечка")
    issue.created_at -= timedelta(hours=25)

    assert [item.id for item in service.collect_newly_overdue()] == [issue.id]
    assert service.collect_newly_overdue() == []


def test_stats_cover_pilot_metrics(service, house) -> None:
    confirmed = waiting_issue(service, house, participants=3)
    service.vote(confirmed.id, 1, True)
    service.vote(confirmed.id, 2, True)
    disputed = waiting_issue(service, house, participants=1, category="свет")
    service.vote(disputed.id, 1, False)
    confirmed_issue = service.repository.get(confirmed.id)
    repeat = new_issue(service, house, "лифт", author=9)
    repeat.created_at = confirmed_issue.confirmed_at + timedelta(days=3)

    stats = service.stats(house.org_id)

    assert stats["total_issues"] == 3
    assert stats["total_reports"] == 5
    assert stats["duplicate_share"] == 40
    assert stats["repeat_issues"] == 1
    assert stats["confirmed_share"] == 50
    assert stats["disputed_share"] == 50
    assert stats["avg_to_confirmed_hours"] is not None
    assert stats["houses"][0]["house"] == "ул. Ленина, 10"
