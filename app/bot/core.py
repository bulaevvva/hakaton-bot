"""Ядро бота: транспорт MAX, маршрутизация, черновики, роли и очередь уведомлений."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import timedelta
from typing import Any, Callable

from ..domain import (
    ACTIVE_STATUSES,
    QUEUE_ROLES,
    House,
    Issue,
    IssueService,
    IssueStatus,
    Role,
    StaffMember,
    category_rule,
    format_issue,
    is_overdue,
    official_appeal_text,
)
from ..max_client import MaxApiError, MaxBotClient
from . import keyboards as kb
from .keyboards import Keyboard, Photo

logger = logging.getLogger("app.bot")

PAGE_SIZE = 8
BOT_LINK_PLACEHOLDER = "https://max.ru/your_bot"

BOT_COMMANDS = [
    {"name": "start", "description": "Главное меню"},
    {"name": "new", "description": "Сообщить о проблеме в доме"},
    {"name": "house", "description": "Проблемы моего дома"},
    {"name": "mine", "description": "Мои заявки"},
    {"name": "settings", "description": "Мой дом и оповещения"},
    {"name": "help", "description": "Как это работает"},
]

Reply = tuple[str, Keyboard | None]


@dataclass
class Draft:
    """Состояние многошагового диалога; хранится в БД и переживает перезапуск."""

    step: str = ""
    house_id: str | None = None
    entrance: str | None = None
    category: str = ""
    description: str = ""
    photos: list[Photo] = field(default_factory=list)
    similar_issue_id: str | None = None
    issue_id: str | None = None
    next: str = ""
    payload: str = ""
    value: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Draft":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Outgoing:
    kind: str  # user | chat | poster
    target: int | str
    text: str = ""
    attachments: Keyboard | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class BotCore:
    def __init__(
        self,
        client: MaxBotClient | None,
        issue_service: IssueService,
        uk_operator_ids: frozenset[int] = frozenset(),
        uk_access_code: str = "",
        confirmation_timeout_minutes: int = 2880,
        *,
        uk_admin_ids: frozenset[int] = frozenset(),
        platform_admin_ids: frozenset[int] = frozenset(),
        bot_link: str = "",
        org_name: str = "УК «Демо»",
        default_org_id: str = "default",
        official_appeal_url: str = "https://dom.gosuslugi.ru",
    ) -> None:
        self.client = client
        self.issue_service = issue_service
        self.repository = issue_service.repository
        self.directory = issue_service.directory
        self.uk_access_code = uk_access_code
        self.platform_admin_ids = platform_admin_ids
        self.confirmation_timeout = timedelta(minutes=confirmation_timeout_minutes)
        self.bot_link = bot_link.rstrip("/")
        self.bot_username = ""
        self.bot_user_id: int | None = None
        self.official_appeal_url = official_appeal_url
        self.default_org = self.directory.ensure_org(org_name, default_org_id)
        for user_id in uk_operator_ids:
            if self.directory.staff(user_id) is None:
                self.directory.add_staff(user_id, self.default_org.id, Role.DISPATCHER)
        for user_id in uk_admin_ids:
            self.directory.add_staff(user_id, self.default_org.id, Role.ADMIN)
        self.drafts: dict[int, Draft] = {}
        self.pending_keyboards: dict[int, Keyboard] = {}
        self.outbox: dict[int, list[Outgoing]] = {}

    # ------------------------------------------------------------------ цикл

    async def run(self, polling_timeout: int) -> None:
        if self.client is None:
            raise RuntimeError("MAX client is required for polling")
        marker: int | None = None
        me = await self.client.get_me()
        self.bot_username = str(me.get("username") or "")
        self.bot_user_id = me.get("user_id")
        if not self.bot_link and self.bot_username:
            self.bot_link = f"https://max.ru/{self.bot_username}"
        try:
            await self.client.set_commands(BOT_COMMANDS)
        except MaxApiError:
            logger.warning("Не удалось обновить меню команд бота", exc_info=True)
        logger.info("Бот MAX запущен: %s", self.bot_username or me.get("name"))
        while True:
            try:
                await self.run_periodic()
                updates, next_marker = await self.client.get_updates(
                    marker=marker, timeout=polling_timeout
                )
                if next_marker is not None:
                    marker = next_marker
                for update in updates:
                    try:
                        await self.handle_update(update)
                    except MaxApiError:
                        logger.exception("Не удалось обработать событие MAX")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка обработки цикла MAX API; повтор через 3 секунды")
                await asyncio.sleep(3)

    async def handle_update(self, update: dict[str, Any]) -> str | None:
        update_type = update.get("update_type")
        if update_type == "message_callback":
            return await self._handle_callback_update(update)
        if update_type not in {None, "message_created", "bot_started"}:
            return None
        message = update.get("message") or {}
        user = update.get("user") or message.get("sender") or {}
        user_id = user.get("user_id")
        if user_id is None:
            return None
        user_id = int(user_id)
        name = str(user.get("name") or user.get("first_name") or "")

        recipient = message.get("recipient") or {}
        if recipient.get("chat_type") in {"chat", "channel"}:
            return await self._handle_group_message(user_id, recipient, message)

        if update_type == "bot_started":
            reply = self._with_draft(user_id, lambda: self.on_start(user_id, str(update.get("payload") or ""), name))
        else:
            text = self._message_text(message).strip()
            photos = self._photos(message)
            location = self._location(message)
            if not text and not photos and location is None:
                return None
            reply = self._handle_text(user_id, text, photos=photos, location=location, name=name)
        await self._send(user_id, reply, self._take_keyboard(user_id))
        await self.flush_outbox(user_id)
        return reply

    async def _handle_callback_update(self, update: dict[str, Any]) -> str | None:
        callback = update.get("callback") or {}
        user = callback.get("user") or update.get("user") or {}
        user_id = user.get("user_id")
        if user_id is None:
            return None
        user_id = int(user_id)
        payload = str(callback.get("payload") or "")
        reply, attachments = self._handle_action(user_id, payload)
        await self._reply_to_callback(user_id, callback.get("callback_id"), reply, attachments)
        await self.flush_outbox(user_id)
        return reply

    async def _handle_group_message(
        self, user_id: int, recipient: dict[str, Any], message: dict[str, Any]
    ) -> str | None:
        """В чате дома бот понимает одну команду: /link КОД — привязать чат к дому."""
        chat_id = recipient.get("chat_id")
        text = self._message_text(message).strip()
        command, _, argument = text.partition(" ")
        if chat_id is None or command.lower().split("@")[0] != "/link":
            return None
        house = self.directory.link_chat(argument, int(chat_id))
        reply = (
            f"✅ Чат привязан к дому {house.address}. Сюда будут приходить оповещения "
            "о проблемах, которые касаются всех соседей, — присоединиться можно одной кнопкой."
            if house else "Код не подошёл. Получите новый у администратора УК в карточке дома."
        )
        if self.client is not None:
            await self.client.send_chat_message(int(chat_id), reply)
        return reply

    async def _reply_to_callback(
        self, user_id: int, callback_id: Any, reply: str, attachments: Keyboard | None
    ) -> None:
        """Нажатие кнопки меняет исходное сообщение, а не добавляет новое в чат."""
        if self.client is None:
            return
        if not callback_id:
            if reply:
                await self._send(user_id, reply, attachments)
            return
        if not reply:
            await self.client.answer_callback(str(callback_id))
            return
        try:
            await self.client.answer_callback(
                str(callback_id), text=reply, attachments=attachments or []
            )
        except MaxApiError:
            logger.warning("Не удалось изменить сообщение, отправляю новое", exc_info=True)
            try:
                await self.client.answer_callback(str(callback_id))
            except MaxApiError:
                pass
            await self._send(user_id, reply, attachments)

    # ------------------------------------------------------------------ маршрутизация

    def _with_draft(self, user_id: int, handler: Callable[[], Any]) -> Any:
        self._load_draft(user_id)
        try:
            return handler()
        finally:
            self._persist_draft(user_id)

    def _handle_text(
        self,
        user_id: int,
        text: str,
        *,
        photos: list[Photo] | None = None,
        location: tuple[float, float] | None = None,
        name: str = "",
    ) -> str:
        return self._with_draft(
            user_id, lambda: self._process_text(user_id, text.strip(), photos or [], location, name)
        )

    def _handle_action(self, user_id: int, payload: str) -> Reply:
        def process() -> Reply:
            reply = self._process_action(user_id, payload)
            return reply[0], reply[1] if reply[1] is not None else self._take_keyboard(user_id)
        return self._with_draft(user_id, process)

    def _process_text(
        self,
        user_id: int,
        text: str,
        photos: list[Photo],
        location: tuple[float, float] | None,
        name: str,
    ) -> str:
        command, _, argument = text.partition(" ")
        command = command.lower().split("@")[0]
        argument = argument.strip()

        if command == "/start":
            return self.on_start(user_id, argument, name)
        if command == "/myid":
            return f"Ваш MAX user_id: {user_id}"
        if command == "/uk":
            return self.enter_demo_admin(user_id, argument, name)
        if command in {"/org_new", "/orgs"}:
            return self.platform_command(user_id, command, argument)
        if not self.has_access(user_id):
            return self.consent_prompt(user_id, "")

        commands: dict[str, Callable[[], str]] = {
            "/new": lambda: self.start_report(user_id),
            "/house": lambda: self.house_issues_view(user_id),
            "/mine": lambda: self.my_issues_view(user_id),
            "/settings": lambda: self.settings_view(user_id),
            "/help": self.help_text,
        }
        if command in commands:
            self.drafts.pop(user_id, None)
            return commands[command]()
        if command in {"/status", "/take", "/done", "/confirm", "/dispute"}:
            return self.issue_command(user_id, command, argument)

        draft = self.drafts.get(user_id)
        if draft is not None and draft.step:
            if draft.step.startswith("res_"):
                return self.resident_step(user_id, draft, text, photos, location)
            if draft.step.startswith("uk_"):
                return self.staff_step(user_id, draft, text, photos)
            if draft.step.startswith("adm_"):
                return self.admin_step(user_id, draft, text, location)
        if location is not None:
            return self.choose_house_by_location(user_id, location, next_action="home")
        if photos:
            self._set_keyboard(user_id, self.main_keyboard(user_id))
            return "Чтобы приложить фото, начните обращение: «➕ Сообщить о проблеме»."
        self._set_keyboard(user_id, self.main_keyboard(user_id))
        return "Выберите действие в меню ниже."

    def _process_action(self, user_id: int, payload: str) -> Reply:
        if payload in {"on:consent", "on:about"}:
            return self.onboarding_action(user_id, payload)
        if not self.has_access(user_id):
            return self.consent_prompt(user_id, ""), self._take_keyboard(user_id)
        draft = self.drafts.get(user_id)
        if draft and draft.step == "uk_result_photo" and not payload.startswith("uk:photo"):
            # Сотрудник ушёл в другой раздел — больше не ждём от него фото результата.
            self.drafts.pop(user_id, None)
        if payload in {"menu:back", "draft:cancel"}:
            self.drafts.pop(user_id, None)
            text = self.home_text(user_id)
            return ("Действие отменено.\n\n" + text if payload == "draft:cancel" else text), self.main_keyboard(user_id)
        if payload == "menu:help":
            return self.help_text(), self.main_keyboard(user_id)
        routes: list[tuple[str, Callable[[int, str], Reply]]] = [
            ("home:", self.house_selection_action),
            ("rep:", self.report_action),
            ("iss:", self.issue_action),
            ("set:", self.settings_action),
            ("menu:", self.menu_action),
            ("uk:", self.staff_action),
            ("mst:", self.staff_action),
            ("adm:", self.admin_action),
            ("chair:", self.chairman_action),
        ]
        for prefix, handler in routes:
            if payload.startswith(prefix):
                return handler(user_id, payload)
        return "Действие устарело. Откройте меню заново.", self.main_keyboard(user_id)

    # Реализуются в миксинах жителя, сотрудника и администратора.
    on_start: Callable[..., str]
    consent_prompt: Callable[..., str]
    onboarding_action: Callable[..., Reply]
    start_report: Callable[..., str]
    house_issues_view: Callable[..., str]
    my_issues_view: Callable[..., str]
    settings_view: Callable[..., str]
    resident_step: Callable[..., str]
    staff_step: Callable[..., str]
    admin_step: Callable[..., str]
    choose_house_by_location: Callable[..., str]
    house_selection_action: Callable[..., Reply]
    report_action: Callable[..., Reply]
    issue_action: Callable[..., Reply]
    settings_action: Callable[..., Reply]
    menu_action: Callable[..., Reply]
    staff_action: Callable[..., Reply]
    admin_action: Callable[..., Reply]
    chairman_action: Callable[..., Reply]
    issue_command: Callable[..., str]
    enter_demo_admin: Callable[..., str]
    platform_command: Callable[..., str]
    home_text: Callable[..., str]
    main_keyboard: Callable[..., Keyboard]
    issue_keyboard: Callable[..., Keyboard]

    # ------------------------------------------------------------------ роли

    def staff(self, user_id: int) -> StaffMember | None:
        return self.directory.staff(user_id)

    def is_queue_staff(self, user_id: int, org_id: str | None = None) -> bool:
        member = self.staff(user_id)
        return (
            member is not None and member.role in QUEUE_ROLES
            and (org_id is None or member.org_id == org_id)
        )

    is_operator = is_queue_staff

    def card(self, user_id: int, issue: Issue) -> str:
        """Карточка заявки с учётом роли: сотрудникам УК — со сроком и приоритетом."""
        member = self.staff(user_id)
        return format_issue(issue, for_staff=member is not None and member.org_id == issue.org_id)

    def is_admin(self, user_id: int) -> bool:
        member = self.staff(user_id)
        return member is not None and member.role is Role.ADMIN

    def is_platform_admin(self, user_id: int) -> bool:
        return user_id in self.platform_admin_ids

    def has_access(self, user_id: int) -> bool:
        """Житель — после согласия на обработку данных; сотрудникам согласие не нужно."""
        return (
            self.staff(user_id) is not None
            or self.is_platform_admin(user_id)
            or self.directory.resident(user_id).consent_at is not None
        )

    # ------------------------------------------------------------------ черновики

    def _load_draft(self, user_id: int) -> None:
        data = self.repository.get_draft(user_id)
        if data is None:
            self.drafts.pop(user_id, None)
        else:
            self.drafts[user_id] = Draft.from_dict(data)

    def _persist_draft(self, user_id: int) -> None:
        draft = self.drafts.get(user_id)
        if draft is None:
            self.repository.delete_draft(user_id)
        else:
            self.repository.save_draft(user_id, asdict(draft))

    def draft(self, user_id: int, step: str, **values: Any) -> Draft:
        current = self.drafts.get(user_id) or Draft()
        current.step = step
        for key, value in values.items():
            setattr(current, key, value)
        self.drafts[user_id] = current
        return current

    # ------------------------------------------------------------------ уведомления

    def _queue(self, actor_id: int | None, item: Outgoing) -> None:
        self.outbox.setdefault(actor_id or 0, []).append(item)

    def to_user(self, actor_id: int | None, user_id: int, text: str, attachments: Keyboard | None) -> None:
        self._queue(actor_id, Outgoing("user", user_id, text, attachments))

    def staff_ids(self, org_id: str) -> list[int]:
        return [
            member.user_id for member in self.directory.list_staff(org_id)
            if member.role in QUEUE_ROLES
        ]

    def notify_staff(
        self,
        actor_id: int | None,
        issue: Issue,
        title: str,
        *,
        with_photos: bool = False,
        skip_participants: bool = False,
    ) -> None:
        """Диспетчерам и администраторам УК, которой принадлежит дом заявки."""
        text = f"{title}\n\n" + format_issue(issue)
        for staff_id in self.staff_ids(issue.org_id):
            if staff_id == actor_id or (skip_participants and staff_id in issue.participants):
                continue
            keyboard = self.issue_keyboard(staff_id, issue)
            if with_photos:
                keyboard = kb.with_photos(issue.photos, keyboard)
            self.to_user(actor_id, staff_id, text, keyboard)

    def notify_participants(
        self,
        actor_id: int | None,
        issue: Issue,
        title: str,
        *,
        exclude: int | None = None,
        recipients: list[int] | None = None,
    ) -> None:
        for participant_id in sorted(recipients if recipients is not None else issue.participants):
            if participant_id == exclude:
                continue
            text = f"{title}\n\n" + self.card(participant_id, issue)
            keyboard = self.issue_keyboard(participant_id, issue)
            if issue.status is IssueStatus.WAITING_CONFIRMATION:
                keyboard = kb.with_photos(issue.result_photos, keyboard)
            self.to_user(actor_id, participant_id, text, keyboard)

    def post_to_house_chat(self, actor_id: int | None, issue: Issue, text: str, *, join_button: bool) -> None:
        """Дублирует массовое оповещение в чат дома, если УК подключила его."""
        if category_rule(issue.category).scope.value == "none":
            return
        house = self.directory.get_house(issue.house_id)
        if house is None or house.chat_id is None:
            return
        attachments = kb.keyboard([[kb.link("🙋 У меня тоже", self.join_link(issue.id))]]) if join_button else None
        self._queue(actor_id, Outgoing("chat", house.chat_id, text, attachments))

    def queue_posters(self, actor_id: int, house: House) -> None:
        for entrance in range(1, house.entrances + 1):
            self._queue(actor_id, Outgoing("poster", actor_id, extra={"house_id": house.id, "entrance": str(entrance)}))

    async def flush_outbox(self, actor_id: int | None) -> None:
        for item in self.outbox.pop(actor_id or 0, []):
            try:
                if item.kind == "user":
                    await self._send(int(item.target), item.text, item.attachments)
                elif item.kind == "chat" and self.client is not None:
                    await self.client.send_chat_message(int(item.target), item.text, item.attachments)
                elif item.kind == "poster":
                    await self._send_poster(int(item.target), item.extra["house_id"], item.extra["entrance"])
            except MaxApiError:
                logger.warning("Не удалось доставить сообщение %s", item.target, exc_info=True)

    async def _send_poster(self, user_id: int, house_id: str, entrance: str) -> None:
        house = self.directory.get_house(house_id)
        if house is None or self.client is None:
            return
        link = self.house_link(house.id, entrance)
        caption = f"🖨 Плакат для печати: {house.address}, подъезд {entrance}\nСсылка в QR: {link}"
        try:
            from ..poster import render_poster

            org = self.directory.get_org(house.org_id)
            png = await asyncio.to_thread(render_poster, house.address, entrance, link, org.name if org else "")
            token = await self.client.upload_image(png, f"qr-{house.id}-{entrance}.png")
        except Exception:
            logger.warning("Не удалось подготовить плакат, отправляю ссылку", exc_info=True)
            await self.client.send_message(user_id, caption)
            return
        await self._send_with_retry(user_id, caption, [kb.image(token)])

    async def _send_with_retry(self, user_id: int, text: str, attachments: Keyboard) -> None:
        # MAX обрабатывает загруженную картинку не мгновенно (attachment.not.ready).
        for attempt in range(4):
            try:
                await self.client.send_message(user_id, text, attachments)
                return
            except MaxApiError:
                if attempt == 3:
                    raise
                await asyncio.sleep(1 + attempt)

    async def _send(self, user_id: int, text: str, attachments: Keyboard | None) -> None:
        if self.client is None:
            return
        try:
            await self.client.send_message(user_id, text, attachments)
        except MaxApiError:
            without_media = [item for item in attachments or [] if item.get("type") != "image"]
            if without_media == (attachments or []):
                raise
            # Токен фото мог устареть — отправляем карточку без изображений.
            await self.client.send_message(user_id, text, without_media)

    # ------------------------------------------------------------------ фоновые задачи

    async def run_periodic(self) -> None:
        service = self.issue_service
        for issue, silent in service.due_reminders(self.confirmation_timeout):
            self.notify_participants(
                None, issue,
                "⏳ Напоминаем: УК сообщила, что проблема устранена. Проверьте и ответьте — "
                "итог решает большинство соседей",
                recipients=silent,
            )
        for issue in service.expire_waiting(self.confirmation_timeout):
            self.announce_outcome(None, issue, by_timeout=True)
        for issue in service.collect_newly_overdue():
            self.notify_staff(
                None, issue,
                f"⏰ Просрочен срок устранения ({category_rule(issue.category).sla_hours} ч). "
                "Заявка поднята наверх очереди",
            )
            self.notify_participants(
                None, issue,
                "⏰ Срок устранения истёк. Если проблема важна, можно подать официальное "
                "обращение — текст уже подготовлен в карточке",
            )
        await self.flush_outbox(None)

    def announce_outcome(self, actor_id: int | None, issue: Issue, *, by_timeout: bool = False) -> None:
        """Итог подтверждения: участникам, УК и (для массовых проблем) в чат дома."""
        rule = category_rule(issue.category)
        if issue.status is IssueStatus.CONFIRMED:
            self.notify_participants(actor_id, issue, "✅ Жители подтвердили: проблема устранена", exclude=actor_id)
            self.notify_staff(actor_id, issue, "✅ Жители подтвердили выполнение", skip_participants=True)
            self.post_to_house_chat(
                actor_id, issue,
                f"✅ Жители подтвердили: {rule.label.lower()} — устранено ({issue.house}).",
                join_button=False,
            )
        elif issue.status is IssueStatus.DISPUTED:
            repeated = issue.dispute_count >= 2
            self.notify_participants(
                actor_id, issue,
                "⚠️ Большинство жителей считают, что проблема осталась. Заявка возвращена УК"
                + (". Это уже повторно — можно подать официальное обращение" if repeated else ""),
                exclude=actor_id,
            )
            self.notify_staff(
                actor_id, issue,
                ("🚨 Жители повторно оспорили выполнение" if repeated
                 else "⚠️ Жители оспорили выполнение. Заявка вернулась в очередь"),
                skip_participants=True,
            )
        elif issue.status is IssueStatus.COMPLETED_NO_RESPONSE:
            self.notify_participants(actor_id, issue, "⌛ Жители не ответили вовремя — заявка закрыта без подтверждения")
            self.notify_staff(actor_id, issue, "⌛ Подтверждение от жителей не получено", skip_participants=True)
        elif by_timeout:
            return

    # ------------------------------------------------------------------ ссылки и клавиатуры

    def _base_link(self) -> str:
        return self.bot_link or BOT_LINK_PLACEHOLDER

    def house_link(self, house_id: str, entrance: str) -> str:
        return f"{self._base_link()}?start=h_{house_id}_{entrance}"

    def join_link(self, issue_id: str) -> str:
        return f"{self._base_link()}?start=join_{issue_id}"

    def invite_link(self, code: str) -> str:
        return f"{self._base_link()}?start=staff_{code}"

    def appeal_buttons(self, issue: Issue) -> list[list[dict[str, Any]]]:
        return [
            [kb.clipboard("📄 Скопировать текст обращения", official_appeal_text(issue))],
            [kb.link("🏛 Подать официальное обращение", self.official_appeal_url)],
        ]

    def needs_appeal(self, issue: Issue) -> bool:
        return issue.status in ACTIVE_STATUSES and (is_overdue(issue) or issue.dispute_count >= 2)

    def _set_keyboard(self, user_id: int, keyboard: Keyboard) -> None:
        self.pending_keyboards[user_id] = keyboard

    def _take_keyboard(self, user_id: int) -> Keyboard:
        keyboard = self.pending_keyboards.pop(user_id, None)
        return keyboard if keyboard is not None else self.main_keyboard(user_id)

    @staticmethod
    def paginate(items: list[Any], page: int) -> tuple[list[Any], int, int]:
        pages = max(1, -(-len(items) // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, pages

    @staticmethod
    def help_text() -> str:
        return (
            "Как это работает\n\n"
            "1. Выберите свой дом — по QR-коду в подъезде, геолокации или адресу.\n"
            "2. «Сообщить о проблеме»: если соседи уже сообщили, нажмите «У меня тоже» — "
            "писать ничего не нужно.\n"
            "3. Следите за статусом: УК отвечает всем участникам сразу и называет срок.\n"
            "4. Когда УК отметит выполнение, подтвердите результат или сообщите, что "
            "проблема осталась. Итог решает большинство соседей.\n"
            "5. Если УК нарушает срок, бот подготовит текст официального обращения.\n\n"
            "Бот не заменяет официальные каналы («Госуслуги Дом», ГИС ЖКХ), а помогает "
            "соседям и УК решать общедомовые проблемы без дублей."
        )

    # ------------------------------------------------------------------ разбор сообщений

    @staticmethod
    def to_int(value: str) -> int:
        try:
            return int(value)
        except ValueError:
            return 0

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        body = message.get("body") or {}
        return body.get("text") or message.get("text") or ""

    @staticmethod
    def _photos(message: dict[str, Any]) -> list[Photo]:
        body = message.get("body") or {}
        photos: list[Photo] = []
        for item in body.get("attachments") or []:
            if not isinstance(item, dict) or item.get("type") not in {"image", "photo"}:
                continue
            payload = item.get("payload") or {}
            photo = {
                key: str(payload[key])
                for key in ("token", "url", "photo_id")
                if isinstance(payload, dict) and payload.get(key)
            }
            photos.append(photo or {"kind": "image"})
        return photos

    @staticmethod
    def _location(message: dict[str, Any]) -> tuple[float, float] | None:
        body = message.get("body") or {}
        for item in body.get("attachments") or []:
            if not isinstance(item, dict) or item.get("type") != "location":
                continue
            source = item if "latitude" in item else (item.get("payload") or {})
            try:
                return float(source["latitude"]), float(source["longitude"])
            except (KeyError, TypeError, ValueError):
                return None
        return None
