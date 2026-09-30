import asyncio
from datetime import timedelta

import pytest

from app.bot import PAGE_SIZE, BotApplication
from app.domain import IssueService, IssueStatus, Role
from app.max_client import MaxApiError
from app.store import InMemoryIssueRepository, SqliteIssueRepository

ADMIN, DISPATCHER, MASTER = 90, 91, 92


class FakeClient:
    """Записывает всё, что бот отправил в MAX."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, list | None]] = []
        self.chat: list[tuple[int, str, list | None]] = []
        self.edits: list[tuple[str, str, list]] = []
        self.uploads = 0

    async def send_message(self, user_id: int, text: str, attachments=None) -> dict:
        self.sent.append((user_id, text, attachments))
        return {}

    async def send_chat_message(self, chat_id: int, text: str, attachments=None) -> dict:
        self.chat.append((chat_id, text, attachments))
        return {}

    async def answer_callback(self, callback_id: str, text=None, attachments=None) -> dict:
        if text:
            self.edits.append((callback_id, text, attachments or []))
        return {}

    async def upload_image(self, content: bytes, filename: str = "") -> str:
        self.uploads += 1
        return f"poster-{self.uploads}"

    def to(self, user_id: int) -> list[str]:
        return [text for recipient, text, _ in self.sent if recipient == user_id]

    def last_attachments(self, user_id: int) -> list:
        return [attachments for recipient, _, attachments in self.sent if recipient == user_id][-1]


class World:
    """Бот с демо-УК, двумя домами и сотрудниками."""

    def __init__(self, repository=None, **options) -> None:
        self.client = FakeClient()
        self.repository = repository or InMemoryIssueRepository()
        self.bot = BotApplication(
            self.client, IssueService(self.repository),
            bot_link="https://max.ru/dom_bot", uk_access_code="secret", **options,
        )
        directory = self.bot.directory
        org = self.bot.default_org
        self.house = directory.get_house("h1") or self.repository.put(
            directory.add_house(org.id, "ул. Ленина, 10", 3, 55.7512, 37.6184)
        )
        self.other = directory.add_house(org.id, "пр-т Мира, 5", 2, 55.7801, 37.6325)
        directory.add_staff(ADMIN, org.id, Role.ADMIN, "Админ")
        directory.add_staff(DISPATCHER, org.id, Role.DISPATCHER, "Ольга")
        directory.add_staff(MASTER, org.id, Role.MASTER, "Иван")

    # события MAX

    def text(self, user_id: int, text: str, attachments=None) -> str | None:
        return asyncio.run(self.bot.handle_update({
            "update_type": "message_created",
            "user": {"user_id": user_id},
            "message": {"body": {"text": text, "attachments": attachments or []},
                        "recipient": {"chat_type": "dialog"}},
        }))

    def press(self, user_id: int, payload: str) -> str:
        asyncio.run(self.bot.handle_update({
            "update_type": "message_callback",
            "callback": {"callback_id": "cb", "payload": payload, "user": {"user_id": user_id}},
        }))
        return self.client.edits[-1][1]

    def keyboard(self) -> list[str]:
        attachments = self.client.edits[-1][2]
        return buttons(attachments)

    def start(self, user_id: int, payload: str = "") -> str | None:
        return asyncio.run(self.bot.handle_update({
            "update_type": "bot_started", "user": {"user_id": user_id}, "payload": payload,
        }))

    def group(self, chat_id: int, user_id: int, text: str) -> str | None:
        return asyncio.run(self.bot.handle_update({
            "update_type": "message_created",
            "user": {"user_id": user_id},
            "message": {"body": {"text": text}, "recipient": {"chat_id": chat_id, "chat_type": "chat"}},
        }))

    # готовые шаги

    def resident(self, user_id: int, entrance: str = "1", house=None) -> None:
        self.start(user_id, f"h_{(house or self.house).id}_{entrance}")
        self.press(user_id, "on:consent")

    def report(self, user_id: int, category: str = "лифт", description: str = "Лифт не едет") -> str:
        self.press(user_id, "menu:new")
        reply = self.press(user_id, f"rep:cat:{category}")
        if "Соседи уже сообщили" in reply:
            self.press(user_id, "rep:new")
        self.text(user_id, description)
        return self.repository.list_for_user(user_id)[0].id

    def issue(self, issue_id: str):
        return self.repository.get(issue_id)

    def periodic(self) -> None:
        asyncio.run(self.bot.run_periodic())


def buttons(attachments: list | None) -> list[str]:
    result = []
    for item in attachments or []:
        if item.get("type") == "inline_keyboard":
            for row in item["payload"]["buttons"]:
                result.extend(button.get("payload") or button.get("url") or button["text"] for button in row)
    return result


@pytest.fixture
def world() -> World:
    return World()


# --- первое знакомство ---

def test_new_user_sees_welcome_and_consent_first(world: World) -> None:
    reply = world.start(10)

    assert "Одна проблема — одна общая заявка" in reply
    assert "on:consent" in buttons(world.client.last_attachments(10))
    world.text(10, "привет")
    assert "on:consent" in buttons(world.client.last_attachments(10))
    assert "uk:take" not in world.press(10, "menu:new")
    assert world.bot.directory.resident(10).consent_at is None


def test_qr_link_saves_house_after_consent(world: World) -> None:
    world.start(10, f"h_{world.house.id}_2")
    reply = world.press(10, "on:consent")

    assert "Ваш дом: ул. Ленина, 10, подъезд 2" in reply
    assert world.bot.directory.resident(10).entrance == "2"


def test_house_selection_by_location(world: World) -> None:
    world.start(10)
    world.press(10, "on:consent")
    reply = world.text(10, "", [{"type": "location", "latitude": 55.7513, "longitude": 37.6186}])

    assert "Ближайшие дома" in reply
    labels = [button["text"] for row in world.client.last_attachments(10)[0]["payload"]["buttons"] for button in row]
    assert labels[0].startswith("🏠 ул. Ленина, 10 ·") and " м" in labels[0]


def test_house_selection_by_search_and_entrance_buttons(world: World) -> None:
    world.start(10)
    world.press(10, "on:consent")
    world.text(10, "ленина д10")
    assert f"home:h:{world.house.id}" in buttons(world.client.last_attachments(10))

    world.press(10, f"home:h:{world.house.id}")
    assert world.keyboard()[:3] == ["home:e:1", "home:e:2", "home:e:3"]
    reply = world.press(10, "home:e:3")
    assert "подъезд 3" in reply


def test_unknown_address_is_not_created_by_resident(world: World) -> None:
    world.start(10)
    world.press(10, "on:consent")
    reply = world.text(10, "Садовая 99")

    assert "Не нашёл такой дом" in reply


# --- обращение, дубли и массовые оповещения ---

def test_report_creates_issue_and_notifies_dispatchers(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10)

    assert world.issue(issue_id).house_id == world.house.id
    for staff_id in (ADMIN, DISPATCHER):
        assert any(text.startswith("🆕 Новая заявка") for text in world.client.to(staff_id))
    assert f"uk:take:{issue_id}" in buttons(world.client.last_attachments(DISPATCHER))
    assert not world.client.to(MASTER)


def test_neighbor_joins_without_description(world: World) -> None:
    world.resident(10)
    world.resident(11)
    issue_id = world.report(10)

    world.press(11, "menu:new")
    reply = world.press(11, "rep:cat:лифт")
    assert "Соседи уже сообщили" in reply and "rep:join" in world.keyboard()
    reply = world.press(11, "rep:join")

    assert "Вы присоединились" in reply
    assert world.issue(issue_id).participants == {10, 11}
    assert any("Теперь жителей: 2" in text for text in world.client.to(DISPATCHER))


def test_lift_alert_goes_to_same_entrance_only(world: World) -> None:
    world.resident(10, "1")
    world.resident(11, "1")
    world.resident(12, "2")
    issue_id = world.report(10, "лифт")

    alert = [text for text in world.client.to(11) if text.startswith("⚠️ Соседи сообщили")]
    assert alert and "подъезд 1" in alert[0]
    assert not any(text.startswith("⚠️ Соседи сообщили") for text in world.client.to(12))
    assert f"iss:j:{issue_id}:alert" in buttons(world.client.last_attachments(11))


def test_house_wide_alert_and_one_tap_join(world: World) -> None:
    world.resident(10, "1")
    world.resident(12, "3")
    issue_id = world.report(10, "двор", "Не открывается шлагбаум")

    assert any("весь дом" in text for text in world.client.to(12))
    reply = world.press(12, f"iss:j:{issue_id}:alert")

    assert "Вы в общей заявке" in reply
    stats = world.bot.issue_service.stats()
    assert stats["joined_from_alerts"] == 1


def test_muted_resident_gets_no_alerts(world: World) -> None:
    world.resident(10)
    world.resident(11)
    world.press(11, "set:mute")
    world.report(10, "отопление", "Холодные батареи")

    assert not any(text.startswith("⚠️") for text in world.client.to(11))


def test_other_category_creates_separate_issue_without_alerts(world: World) -> None:
    world.resident(10)
    world.resident(11)
    first = world.report(10, "другое", "Сломана лавочка")
    second = world.report(11, "другое", "Нет таблички с номером дома")

    assert first != second
    assert not any(text.startswith("⚠️") for text in world.client.to(11))


def test_photo_before_description_is_attached(world: World) -> None:
    world.resident(10)
    world.press(10, "menu:new")
    world.press(10, "rep:cat:протечка")
    world.text(10, "", [{"type": "image", "payload": {"token": "tok-1"}}])
    world.text(10, "Течёт с потолка у лифта")

    issue = world.repository.list_for_user(10)[0]
    assert issue.photos == [{"token": "tok-1"}]
    assert {"type": "image", "payload": {"token": "tok-1"}} in world.client.last_attachments(DISPATCHER)


def test_house_issues_are_visible_to_neighbors_but_not_other_houses(world: World) -> None:
    world.resident(10)
    world.resident(11)
    world.resident(30, "1", world.other)
    issue_id = world.report(10, "свет", "Темно на лестнице")

    reply = world.press(11, "menu:house")
    assert "Свет в подъезде" in reply
    world.press(11, f"iss:v:{issue_id}")
    assert f"iss:j:{issue_id}:house" in world.keyboard()
    assert "доступна жителям её дома" in world.press(30, f"iss:v:{issue_id}")


def test_join_link_from_neighbor(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10)
    card_buttons = world.client.last_attachments(10)
    clipboard = [
        button for item in card_buttons if item["type"] == "inline_keyboard"
        for row in item["payload"]["buttons"] for button in row if button["type"] == "clipboard"
    ]
    assert f"start=join_{issue_id}" in clipboard[0]["payload"]

    world.start(40, f"join_{issue_id}")
    reply = world.press(40, "on:consent")
    assert "зовут вас присоединиться" in reply
    world.press(40, f"iss:j:{issue_id}:link")
    assert 40 in world.issue(issue_id).participants
    assert world.bot.directory.resident(40).house_id == world.house.id


# --- работа УК ---

def test_dispatcher_takes_sets_plan_and_comment(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10)

    world.press(DISPATCHER, f"uk:take:{issue_id}")
    assert "взяла заявку в работу" in world.client.to(10)[-1]
    world.press(DISPATCHER, f"uk:pl:{issue_id}:1")
    assert "назначила срок устранения" in world.client.to(10)[-1]
    world.press(DISPATCHER, f"uk:cmt:{issue_id}")
    world.text(DISPATCHER, "Мастер придёт завтра с 10 до 12")

    assert "Мастер придёт завтра" in world.client.to(10)[-1]
    issue = world.issue(issue_id)
    assert issue.status is IssueStatus.IN_PROGRESS and issue.planned_at is not None


def test_master_assignment_and_completion_with_photo(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10)

    world.press(DISPATCHER, f"uk:asg:{issue_id}")
    assert f"uk:asg2:{issue_id}:{MASTER}" in world.keyboard()
    world.press(DISPATCHER, f"uk:asg2:{issue_id}:{MASTER}")
    assert "Вам назначена заявка" in world.client.to(MASTER)[-1]
    assert f"uk:done:{issue_id}" in buttons(world.client.last_attachments(MASTER))

    world.press(MASTER, f"uk:done:{issue_id}")
    assert "итог решает большинство" in world.client.to(10)[-1]
    assert any("Исполнитель отметил выполнение" in text for text in world.client.to(DISPATCHER))

    world.text(MASTER, "", [{"type": "image", "payload": {"token": "after-1"}}])
    assert world.issue(issue_id).result_photos == [{"token": "after-1"}]
    assert {"type": "image", "payload": {"token": "after-1"}} in world.client.last_attachments(10)


def test_master_cannot_take_or_complete_foreign_issue(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10)

    assert "диспетчеру или администратору" in world.press(MASTER, f"uk:take:{issue_id}")
    assert "назначенный исполнитель" in world.press(MASTER, f"uk:done:{issue_id}")


def test_resident_has_no_access_to_uk_actions(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10)

    assert "сотрудникам" in world.press(10, f"uk:take:{issue_id}")
    assert "администратору УК" in world.press(10, "adm:menu")


def test_queue_is_sorted_by_priority_and_paginated(world: World) -> None:
    for index in range(PAGE_SIZE + 1):
        world.resident(100 + index, str(index % 3 + 1))
        world.report(100 + index, "свет", f"Темно на этаже {index}")
    world.resident(10)
    leak_id = world.report(10, "протечка", "Течёт крыша")

    reply = world.press(DISPATCHER, "menu:uk")
    first_line = [line for line in reply.splitlines() if "·" in line and "п." in line][0]
    assert "Протечка" in first_line
    assert "uk:list:active:1" in world.keyboard()
    assert leak_id in {issue.id for issue in world.bot.issue_service.queue(world.house.org_id)[:1]}


# --- подтверждение жителями ---

def completed_issue(world: World, residents: list[int]) -> str:
    for user_id in residents:
        world.resident(user_id)
    issue_id = world.report(residents[0])
    for user_id in residents[1:]:
        world.press(user_id, f"iss:j:{issue_id}:house")
    world.press(DISPATCHER, f"uk:done:{issue_id}")
    world.press(DISPATCHER, "uk:photo_skip")
    return issue_id


def test_majority_confirms_and_staff_is_notified(world: World) -> None:
    issue_id = completed_issue(world, [10, 11, 12])

    reply = world.press(10, f"iss:ok:{issue_id}")
    assert "ответ учтён" in reply and "✅ 1 · ❌ 0 из 3" in reply
    reply = world.press(11, f"iss:ok:{issue_id}")

    assert "подтвердили выполнение" in reply
    assert world.issue(issue_id).status is IssueStatus.CONFIRMED
    assert "проблема устранена" in world.client.to(12)[-1]
    assert any("Жители подтвердили выполнение" in text for text in world.client.to(DISPATCHER))


def test_minority_dispute_warns_staff_without_reopening(world: World) -> None:
    issue_id = completed_issue(world, [10, 11, 12])

    world.press(10, f"iss:no:{issue_id}")

    assert world.issue(issue_id).status is IssueStatus.WAITING_CONFIRMATION
    assert any("проблема осталась (1 из 3)" in text for text in world.client.to(DISPATCHER))


def test_repeated_dispute_offers_official_appeal(world: World) -> None:
    issue_id = completed_issue(world, [10])
    world.press(10, f"iss:no:{issue_id}")
    world.press(DISPATCHER, f"uk:done:{issue_id}")
    world.press(DISPATCHER, "uk:photo_skip")
    world.press(10, f"iss:no:{issue_id}")

    assert world.issue(issue_id).dispute_count == 2
    assert any("повторно оспорили" in text for text in world.client.to(DISPATCHER))
    world.press(10, f"iss:v:{issue_id}")
    assert "https://dom.gosuslugi.ru" in world.keyboard()


def test_chairman_invite_and_decisive_vote(world: World) -> None:
    world.press(ADMIN, f"adm:chair:{world.house.id}")
    link = world.client.edits[-1][1].split("\n")[1]
    world.start(60, link.split("start=")[1])
    issue_id = completed_issue(world, [10, 11, 12])

    reply = world.press(60, f"iss:ok:{issue_id}")

    assert "председателя совета дома" in reply
    assert world.issue(issue_id).status is IssueStatus.CONFIRMED


def test_reminder_and_timeout_via_periodic_job(world: World) -> None:
    issue_id = completed_issue(world, [10, 11, 12])
    world.press(10, f"iss:ok:{issue_id}")
    issue = world.issue(issue_id)
    issue.completed_at -= timedelta(hours=25)
    world.repository.save(issue)

    world.periodic()
    assert world.client.to(11)[-1].startswith("⏳ Напоминаем")
    assert not world.client.to(10)[-1].startswith("⏳")

    issue = world.issue(issue_id)
    issue.completed_at -= timedelta(hours=24)
    world.repository.save(issue)
    world.periodic()
    assert world.issue(issue_id).status is IssueStatus.CONFIRMED


def test_overdue_escalation_offers_appeal_to_residents(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10, "протечка", "Течёт крыша")
    issue = world.issue(issue_id)
    issue.created_at -= timedelta(hours=25)
    world.repository.save(issue)

    world.periodic()
    world.periodic()

    assert len([text for text in world.client.to(DISPATCHER) if "Просрочен срок" in text]) == 1
    assert "официальное обращение" in world.client.to(10)[-1]
    assert "https://dom.gosuslugi.ru" in buttons(world.client.last_attachments(10))


# --- администратор УК ---

def test_admin_adds_house_with_geolocation(world: World) -> None:
    world.press(ADMIN, "adm:add")
    world.text(ADMIN, "ул. Садовая, 3к1")
    world.text(ADMIN, "2")
    reply = world.text(ADMIN, "", [{"type": "location", "latitude": 55.765, "longitude": 37.609}])

    assert "Дом добавлен" in reply
    house = world.bot.directory.find_house_by_key(world.house.org_id, "Садовая 3 корп 1")
    assert house is not None and house.entrances == 2 and house.latitude == 55.765


def test_admin_cannot_duplicate_house(world: World) -> None:
    world.press(ADMIN, "adm:add")
    reply = world.text(ADMIN, "Ленина д. 10")

    assert "уже есть в справочнике" in reply


def test_admin_gets_qr_posters_as_images(world: World) -> None:
    world.press(ADMIN, f"adm:qr:{world.house.id}")

    posters = [(text, attachments) for user, text, attachments in world.client.sent if user == ADMIN and "Плакат" in text]
    assert len(posters) == world.house.entrances
    assert posters[0][1] == [{"type": "image", "payload": {"token": "poster-1"}}]
    assert f"start=h_{world.house.id}_1" in posters[0][0]


def test_poster_failure_falls_back_to_link(world: World) -> None:
    async def broken_upload(content, filename=""):
        raise MaxApiError("upload failed")

    world.client.upload_image = broken_upload
    world.press(ADMIN, f"adm:qr:{world.house.id}")

    texts = [text for text in world.client.to(ADMIN) if "Плакат" in text]
    assert len(texts) == world.house.entrances and "start=h_" in texts[0]


def test_admin_invites_and_removes_staff(world: World) -> None:
    world.press(ADMIN, "adm:inv:dispatcher")
    link = world.client.edits[-1][1].split("\n")[1]
    reply = world.start(70, link.split("start=")[1])

    assert "диспетчер" in reply
    assert world.bot.staff(70).role is Role.DISPATCHER
    assert "использовано" in world.start(71, link.split("start=")[1])

    world.press(ADMIN, "adm:rm:70")
    world.press(ADMIN, "adm:rm2:70")
    assert world.bot.staff(70) is None


def test_admin_metrics_show_pilot_metrics(world: World) -> None:
    completed_issue(world, [10, 11])
    reply = world.press(ADMIN, "adm:stats")

    for marker in ("Доля дублей", "Повторных обращений", "Подтверждено жителями", "Оспорено", "подтверждённого устранения"):
        assert marker in reply


def test_house_chat_link_and_alerts(world: World) -> None:
    reply = world.press(ADMIN, f"adm:chat:{world.house.id}")
    code = reply.split("/link ")[1].split()[0]
    assert "Код не подошёл" in world.group(-500, 5, "/link WRONG")
    assert "Чат привязан" in world.group(-500, 5, f"/link {code}")

    world.resident(10)
    issue_id = world.report(10, "отопление", "Холодные батареи")

    chat_id, text, attachments = world.client.chat[-1]
    assert chat_id == -500 and "Холодные батареи" in text
    assert f"https://max.ru/dom_bot?start=join_{issue_id}" in buttons(attachments)


def test_announcement_reaches_house_residents(world: World) -> None:
    world.resident(10)
    world.resident(11, "2")
    world.resident(30, "1", world.other)

    world.press(DISPATCHER, "uk:announce")
    world.press(DISPATCHER, f"uk:ann:{world.house.id}")
    reply = world.text(DISPATCHER, "Завтра с 10 до 14 отключат горячую воду")

    assert "жителям дома ул. Ленина, 10: 2" in reply
    assert "отключат горячую воду" in world.client.to(11)[-1]
    assert not any("отключат" in text for text in world.client.to(30))


# --- демо, сервис, надёжность ---

def test_demo_code_grants_admin_role(world: World) -> None:
    assert "Код не принят" in world.text(80, "/uk wrong")
    reply = world.text(80, "/uk secret")

    assert "администратор" in reply
    assert world.bot.is_admin(80)


def test_platform_admin_connects_new_organization() -> None:
    world = World(platform_admin_ids=frozenset({1}))
    assert "администратору сервиса" in world.text(5, "/org_new УК «Новая»")
    reply = world.text(1, "/org_new УК «Новая»")
    code = reply.split("start=staff_")[1].split()[0]

    world.start(6, f"staff_{code}")
    staff = world.bot.staff(6)
    assert staff.role is Role.ADMIN and staff.org_id != world.bot.default_org.id


def test_draft_survives_restart(tmp_path) -> None:
    repository = SqliteIssueRepository(str(tmp_path / "bot.db"))
    first = World(repository)
    first.resident(10)
    first.press(10, "menu:new")
    first.press(10, "rep:cat:свет")

    second = World(repository)
    reply = second.text(10, "Не горит лампа на втором этаже")
    repository.close()

    assert "Заявка создана" in reply


def test_failed_edit_falls_back_to_new_message(world: World) -> None:
    async def failing_answer(callback_id, text=None, attachments=None):
        if text:
            raise MaxApiError("message too old")
        return {}

    world.resident(10)
    world.client.answer_callback = failing_answer
    asyncio.run(world.bot.handle_update({
        "update_type": "message_callback",
        "callback": {"callback_id": "cb", "payload": "menu:help", "user": {"user_id": 10}},
    }))

    assert "Как это работает" in world.client.to(10)[-1]


def test_legacy_text_commands_still_work(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10)

    assert "в работе" in world.text(DISPATCHER, f"/take {issue_id}")
    world.text(DISPATCHER, f"/done {issue_id}")
    assert "подтвердили" in world.text(10, f"/confirm {issue_id}")


def test_resident_card_hides_staff_only_lines(world: World) -> None:
    world.resident(10)
    issue_id = world.report(10)

    resident_view = world.press(10, f"iss:v:{issue_id}")
    staff_view = world.press(DISPATCHER, f"iss:v:{issue_id}")

    assert "Приоритет" not in resident_view and "Норматив" not in resident_view
    assert "Приоритет" in staff_view and "Норматив" in staff_view
