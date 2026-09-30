from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol, TypeVar
from uuid import uuid4

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def short_id(length: int = 8) -> str:
    return uuid4().hex[:length]


# ---------------------------------------------------------------- статусы и роли


class IssueStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_CONFIRMATION = "waiting_confirmation"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"
    COMPLETED_NO_RESPONSE = "completed_no_response"
    DONE = "done"  # legacy value for records created by the first MVP


ACTIVE_STATUSES = frozenset({IssueStatus.OPEN, IssueStatus.IN_PROGRESS, IssueStatus.DISPUTED})
FINISHED_STATUSES = frozenset({
    IssueStatus.CONFIRMED,
    IssueStatus.COMPLETED_NO_RESPONSE,
    IssueStatus.DONE,
})

STATUS_LABELS = {
    IssueStatus.OPEN: "новая",
    IssueStatus.IN_PROGRESS: "в работе",
    IssueStatus.WAITING_CONFIRMATION: "ожидает подтверждения жителей",
    IssueStatus.CONFIRMED: "подтверждено жителями",
    IssueStatus.DISPUTED: "оспорено жителями",
    IssueStatus.COMPLETED_NO_RESPONSE: "выполнено УК, подтверждение не получено",
    IssueStatus.DONE: "выполнено УК",
}


class Role(StrEnum):
    ADMIN = "admin"
    DISPATCHER = "dispatcher"
    MASTER = "master"


ROLE_LABELS = {
    Role.ADMIN: "администратор УК",
    Role.DISPATCHER: "диспетчер",
    Role.MASTER: "исполнитель",
}
# Кто может работать с очередью заявок.
QUEUE_ROLES = frozenset({Role.ADMIN, Role.DISPATCHER})
CHAIRMAN_INVITE = "chairman"

# Часовой пояс для сроков в сообщениях; переопределяется из DISPLAY_UTC_OFFSET_HOURS.
DISPLAY_TZ = timezone(timedelta(hours=3))


def set_display_offset(hours: int) -> None:
    global DISPLAY_TZ
    DISPLAY_TZ = timezone(timedelta(hours=hours))


# ---------------------------------------------------------------- категории


class Scope(StrEnum):
    ENTRANCE = "entrance"  # проблема одного подъезда
    HOUSE = "house"  # касается всего дома и двора
    NONE = "none"  # не объединяем и не рассылаем


@dataclass(frozen=True)
class CategoryRule:
    key: str
    label: str
    emoji: str
    weight: int
    sla_hours: int
    scope: Scope


# Порядок совпадает с кнопками. weight — срочность для очереди УК; sla_hours —
# срок устранения по умолчанию (ориентир, УК может задать свои нормативы);
# scope — кого объединяем в одну заявку и кому рассылаем оповещение.
CATEGORIES: tuple[CategoryRule, ...] = (
    CategoryRule("лифт", "Лифт", "🛗", 3, 24, Scope.ENTRANCE),
    CategoryRule("свет", "Свет в подъезде", "💡", 1, 72, Scope.ENTRANCE),
    CategoryRule("мусоропровод", "Мусоропровод", "🗑", 2, 24, Scope.ENTRANCE),
    CategoryRule("протечка", "Протечка, кровля", "💧", 3, 24, Scope.HOUSE),
    CategoryRule("отопление", "Отопление и вода", "🔥", 3, 24, Scope.HOUSE),
    CategoryRule("двор", "Двор и шлагбаум", "🚧", 1, 72, Scope.HOUSE),
    CategoryRule("уборка", "Уборка", "🧹", 1, 48, Scope.HOUSE),
    CategoryRule("другое", "Другое", "🔧", 1, 72, Scope.NONE),
)
_CATEGORY_BY_KEY = {rule.key: rule for rule in CATEGORIES}


def category_rule(category: str) -> CategoryRule:
    key = normalize(category)
    return _CATEGORY_BY_KEY.get(key) or CategoryRule(
        key, category.strip() or "Другое", "🔧", 1, 72, Scope.NONE
    )


# ---------------------------------------------------------------- сущности


@dataclass
class Organization:
    name: str
    id: str = field(default_factory=short_id)
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class StaffMember:
    user_id: int
    org_id: str
    role: Role
    name: str = ""
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class House:
    """Дом из справочника УК: единый адрес, число подъездов, координаты."""

    org_id: str
    address: str
    entrances: int
    id: str = field(default_factory=lambda: short_id(6))
    house_key: str = ""
    latitude: float | None = None
    longitude: float | None = None
    chairman_id: int | None = None
    # Необязательный чат дома в MAX: туда дублируются массовые оповещения.
    chat_id: int | None = None
    chat_link_code: str = ""
    active: bool = True
    created_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if not self.house_key:
            self.house_key = normalize_address(self.address)


@dataclass
class Resident:
    user_id: int
    house_id: str | None = None
    entrance: str | None = None
    consent_at: datetime | None = None
    notify_mass: bool = True
    mass_day: str = ""
    mass_count: int = 0
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class StaffInvite:
    """Одноразовая ссылка: сотрудник УК или председатель совета дома."""

    org_id: str
    role: str
    created_by: int
    house_id: str | None = None
    code: str = field(default_factory=lambda: short_id(12))
    used_by: int | None = None
    used_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Issue:
    house: str
    entrance: str
    category: str
    description: str
    author_id: int
    id: str = field(default_factory=short_id)
    status: IssueStatus = IssueStatus.OPEN
    participants: set[int] = field(default_factory=set)
    confirmations: set[int] = field(default_factory=set)
    photo_note: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    house_key: str = ""
    photos: list[dict[str, str]] = field(default_factory=list)
    result_photos: list[dict[str, str]] = field(default_factory=list)
    taken_at: datetime | None = None
    completed_at: datetime | None = None
    overdue_notified: bool = False
    house_id: str = ""
    org_id: str = ""
    disputes: set[int] = field(default_factory=set)
    dispute_count: int = 0
    reminder_sent: bool = False
    confirmed_at: datetime | None = None
    planned_at: datetime | None = None
    uk_comment: str = ""
    assignee_id: int | None = None

    def __post_init__(self) -> None:
        self.participants.add(self.author_id)
        if not self.house_key:
            self.house_key = normalize_address(self.house)
        if self.photos and not self.photo_note:
            self.photo_note = "image"


@dataclass
class IssueEvent:
    """Журнал: из него считаются метрики пилота."""

    issue_id: str
    kind: str
    actor_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: short_id(12))
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class Announcement:
    house_id: str
    text: str
    author_id: int
    recipients: int = 0
    id: str = field(default_factory=short_id)
    created_at: datetime = field(default_factory=utcnow)


class Repository(Protocol):
    # Общие операции для справочников: Organization, StaffMember, House,
    # Resident, StaffInvite, IssueEvent, Announcement.
    def get_obj(self, cls: type[T], key: Any) -> T | None: ...
    def put(self, obj: Any) -> Any: ...
    def delete_obj(self, cls: type, key: Any) -> None: ...
    def select(self, cls: type[T], **equals: Any) -> list[T]: ...

    # Заявки.
    def find_similar(
        self, house_id: str, entrance: str | None, category: str, now: datetime
    ) -> Issue | None: ...
    def add(self, issue: Issue) -> Issue: ...
    def get(self, issue_id: str) -> Issue | None: ...
    def save(self, issue: Issue) -> Issue: ...
    def list_for_user(self, user_id: int) -> list[Issue]: ...
    def list_for_uk(self, statuses: set[IssueStatus] | None = None) -> list[Issue]: ...
    def list_waiting_confirmation(self) -> list[Issue]: ...

    # Черновики диалогов бота.
    def get_draft(self, user_id: int) -> dict[str, Any] | None: ...
    def save_draft(self, user_id: int, data: dict[str, Any]) -> None: ...
    def delete_draft(self, user_id: int) -> None: ...


# ---------------------------------------------------------------- адреса


def normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


_STREET_TYPES = {
    "пр": "пр-т", "просп": "пр-т", "проспект": "пр-т", "пр-т": "пр-т",
    "пер": "пер", "переулок": "пер",
    "ш": "ш", "шоссе": "ш",
    "б-р": "б-р", "бульвар": "б-р", "бул": "б-р",
    "пл": "пл", "площадь": "пл",
    "наб": "наб", "набережная": "наб",
    "мкр": "мкр", "микрорайон": "мкр",
    "проезд": "проезд", "туп": "туп", "тупик": "туп",
}
_BUILDING_MARKERS = {
    "к": "к", "корп": "к", "корпус": "к",
    "стр": "с", "строение": "с", "с": "с",
    "лит": "лит", "литера": "лит",
}


def normalize_address(value: str) -> str:
    """Ключ дома: «ул. Ленина, д. 10», «Ленина 10» и «улица Ленина дом 10» совпадают.

    Тип улицы (проспект, переулок…) сохраняется и переносится в конец, чтобы
    «пр-т Мира 5» и «Мира пр-т 5» совпадали, а «пер. Мира 5» — нет.
    """
    text = value.lower().replace("ё", "е")
    text = re.sub(r"[,.;:«»\"'()№#/\\]", " ", text)
    text = re.sub(r"(\d)([а-яa-z])", r"\1 \2", text)
    text = re.sub(r"([а-яa-z])(\d)", r"\1 \2", text)
    tokens = text.split()
    if len(tokens) > 2 and tokens[0] in {"г", "город"}:
        tokens = tokens[2:]

    words: list[str] = []
    street_types: list[str] = []
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token in {"ул", "улица"}:
            continue
        if token in {"д", "дом"} and following[:1].isdigit():
            continue
        if token in _STREET_TYPES:
            street_types.append(_STREET_TYPES[token])
            continue
        if token in _BUILDING_MARKERS and following[:1].isdigit() and words:
            words.append(_BUILDING_MARKERS[token])
            continue
        words.append(token)
    return " ".join(words + street_types)


def normalize_entrance(value: str) -> str:
    """«1», «1-й», «подъезд 1» и «п. 1» → «1»."""
    match = re.search(r"\d+", value)
    return match.group() if match else normalize(value)


def display_house(value: str) -> str:
    return " ".join(value.strip().split())[:200]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- сроки и приоритет


def format_local(moment: datetime) -> str:
    return moment.astimezone(DISPLAY_TZ).strftime("%d.%m %H:%M")


def format_day(moment: datetime) -> str:
    return moment.astimezone(DISPLAY_TZ).strftime("%d.%m")


def end_of_local_day(days_ahead: int, now: datetime | None = None) -> datetime:
    local = (now or utcnow()).astimezone(DISPLAY_TZ) + timedelta(days=days_ahead)
    return local.replace(hour=23, minute=59, second=0, microsecond=0).astimezone(timezone.utc)


def sla_deadline(issue: Issue) -> datetime:
    return issue.created_at + timedelta(hours=category_rule(issue.category).sla_hours)


def is_overdue(issue: Issue, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return issue.status in ACTIVE_STATUSES and now > sla_deadline(issue)


def priority_score(issue: Issue, now: datetime | None = None) -> float:
    """Чем больше соседей, срочнее категория и дольше ожидание — тем выше в очереди."""
    now = now or utcnow()
    hours_open = max(0.0, (now - issue.created_at).total_seconds() / 3600)
    score = category_rule(issue.category).weight * 10
    score += (len(issue.participants) - 1) * 5
    score += min(hours_open, 72) / 3
    if is_overdue(issue, now):
        score += 20
    return score


def priority_label(issue: Issue, now: datetime | None = None) -> str:
    score = priority_score(issue, now)
    if score >= 40:
        return "🔴 высокий"
    if score >= 20:
        return "🟡 средний"
    return "🟢 обычный"


# ---------------------------------------------------------------- справочник


class Directory:
    """Организации, сотрудники, дома, жители и приглашения."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    # УК и сотрудники

    def ensure_org(self, name: str, org_id: str) -> Organization:
        org = self.repository.get_obj(Organization, org_id)
        if org is None:
            org = self.repository.put(Organization(name=name, id=org_id))
        return org

    def create_org(self, name: str) -> Organization:
        return self.repository.put(Organization(name=name.strip()[:120]))

    def get_org(self, org_id: str) -> Organization | None:
        return self.repository.get_obj(Organization, org_id)

    def staff(self, user_id: int) -> StaffMember | None:
        return self.repository.get_obj(StaffMember, user_id)

    def add_staff(self, user_id: int, org_id: str, role: Role, name: str = "") -> StaffMember:
        return self.repository.put(StaffMember(user_id=user_id, org_id=org_id, role=Role(role), name=name))

    def remove_staff(self, user_id: int) -> None:
        self.repository.delete_obj(StaffMember, user_id)

    def list_staff(self, org_id: str, role: Role | None = None) -> list[StaffMember]:
        members = self.repository.select(StaffMember, org_id=org_id)
        if role is not None:
            members = [member for member in members if member.role is role]
        return sorted(members, key=lambda member: (member.role.value, member.created_at))

    def create_invite(
        self, org_id: str, role: str, created_by: int, house_id: str | None = None
    ) -> StaffInvite:
        return self.repository.put(
            StaffInvite(org_id=org_id, role=role, created_by=created_by, house_id=house_id)
        )

    def accept_invite(self, code: str, user_id: int, name: str = "") -> StaffInvite | None:
        invite = self.repository.get_obj(StaffInvite, code)
        if invite is None or invite.used_by is not None:
            return None
        if invite.role == CHAIRMAN_INVITE:
            house = self.get_house(invite.house_id or "")
            if house is None:
                return None
            house.chairman_id = user_id
            self.repository.put(house)
            resident = self.resident(user_id)
            if resident.house_id is None:
                self.set_resident_house(user_id, house.id, "1")
        else:
            self.add_staff(user_id, invite.org_id, Role(invite.role), name)
        invite.used_by = user_id
        invite.used_at = utcnow()
        self.repository.put(invite)
        return invite

    # Дома

    def get_house(self, house_id: str) -> House | None:
        return self.repository.get_obj(House, house_id)

    def list_houses(self, org_id: str | None = None) -> list[House]:
        houses = self.repository.select(House, org_id=org_id) if org_id else self.repository.select(House)
        return sorted((house for house in houses if house.active), key=lambda house: house.address)

    def find_house_by_key(self, org_id: str, address: str) -> House | None:
        key = normalize_address(address)
        return next((house for house in self.list_houses(org_id) if house.house_key == key), None)

    def add_house(
        self,
        org_id: str,
        address: str,
        entrances: int,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> House:
        existing = self.find_house_by_key(org_id, address)
        if existing is not None:
            return existing
        return self.repository.put(House(
            org_id=org_id, address=display_house(address), entrances=max(1, min(entrances, 40)),
            latitude=latitude, longitude=longitude,
        ))

    def update_house(self, house: House) -> House:
        house.house_key = normalize_address(house.address)
        return self.repository.put(house)

    def start_chat_link(self, house: House) -> str:
        """Код, который администратор чата отправит в чат дома: /link КОД."""
        house.chat_link_code = short_id(6).upper()
        self.repository.put(house)
        return house.chat_link_code

    def link_chat(self, code: str, chat_id: int) -> House | None:
        code = code.strip().upper()
        house = next(
            (item for item in self.list_houses() if code and item.chat_link_code == code), None
        )
        if house is None:
            return None
        house.chat_id = chat_id
        house.chat_link_code = ""
        return self.repository.put(house)

    def unlink_chat(self, house: House) -> House:
        house.chat_id = None
        house.chat_link_code = ""
        return self.repository.put(house)

    def search_houses(self, query: str, limit: int = 6) -> list[House]:
        """Поиск по словам адреса: «ленина 10» найдёт «ул. Ленина, д. 10»."""
        tokens = normalize_address(query).split()
        if not tokens:
            return []
        scored = []
        for house in self.list_houses():
            key_tokens = house.house_key.split()
            hits = sum(1 for token in tokens if any(part.startswith(token) for part in key_tokens))
            if hits:
                exact = house.house_key == " ".join(tokens)
                scored.append((exact, hits, house))
        scored.sort(key=lambda item: (not item[0], -item[1], item[2].address))
        best = scored[0][1] if scored else 0
        return [house for _, hits, house in scored if hits == best][:limit]

    def nearest_houses(
        self, latitude: float, longitude: float, limit: int = 3, max_distance_m: float = 1500
    ) -> list[tuple[House, float]]:
        located = [
            (house, distance_m(latitude, longitude, house.latitude, house.longitude))
            for house in self.list_houses()
            if house.latitude is not None and house.longitude is not None
        ]
        located = [item for item in located if item[1] <= max_distance_m]
        return sorted(located, key=lambda item: item[1])[:limit]

    # Жители

    def resident(self, user_id: int) -> Resident:
        return self.repository.get_obj(Resident, user_id) or Resident(user_id=user_id)

    def save_resident(self, resident: Resident) -> Resident:
        resident.updated_at = utcnow()
        return self.repository.put(resident)

    def give_consent(self, user_id: int) -> Resident:
        resident = self.resident(user_id)
        resident.consent_at = resident.consent_at or utcnow()
        return self.save_resident(resident)

    def set_resident_house(self, user_id: int, house_id: str, entrance: str) -> Resident:
        resident = self.resident(user_id)
        resident.house_id = house_id
        resident.entrance = normalize_entrance(entrance)
        resident.consent_at = resident.consent_at or utcnow()
        return self.save_resident(resident)

    def residents_in_scope(self, house_id: str, entrance: str | None = None) -> list[Resident]:
        residents = self.repository.select(Resident, house_id=house_id)
        if entrance is not None:
            residents = [resident for resident in residents if resident.entrance == entrance]
        return [resident for resident in residents if resident.consent_at is not None]

    def is_chairman(self, user_id: int, house_id: str) -> bool:
        house = self.get_house(house_id)
        return house is not None and house.chairman_id == user_id

    def chaired_houses(self, user_id: int) -> list[House]:
        return [house for house in self.list_houses() if house.chairman_id == user_id]


# ---------------------------------------------------------------- заявки


MASS_NOTIFY_DAILY_LIMIT = 3
REPEAT_WINDOW = timedelta(days=30)


class VoteResult:
    RECORDED = "recorded"
    CONFIRMED = "confirmed"
    DISPUTED = "disputed"


class IssueService:
    MATCH_WINDOW = timedelta(hours=72)

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.directory = Directory(repository)

    # журнал

    def log(self, issue: Issue, kind: str, actor_id: int | None = None, **data: Any) -> None:
        self.repository.put(IssueEvent(issue_id=issue.id, kind=kind, actor_id=actor_id, data=data))

    def events(self, issue_id: str | None = None) -> list[IssueEvent]:
        events = (
            self.repository.select(IssueEvent, issue_id=issue_id)
            if issue_id else self.repository.select(IssueEvent)
        )
        return sorted(events, key=lambda event: event.created_at)

    # создание и дубли

    def find_similar(self, house_id: str, entrance: str, category: str) -> Issue | None:
        rule = category_rule(category)
        if rule.scope is Scope.NONE:
            return None
        return self.repository.find_similar(
            house_id,
            normalize_entrance(entrance) if rule.scope is Scope.ENTRANCE else None,
            rule.key,
            utcnow(),
        )

    def create(
        self,
        *,
        house: House,
        entrance: str,
        category: str,
        description: str,
        author_id: int,
        photos: list[dict[str, str]] | None = None,
    ) -> Issue:
        now = utcnow()
        issue = self.repository.add(Issue(
            house=house.address,
            house_id=house.id,
            house_key=house.house_key,
            org_id=house.org_id,
            entrance=normalize_entrance(entrance),
            category=category_rule(category).key,
            description=description.strip()[:1000],
            author_id=author_id,
            photos=list(photos or []),
            created_at=now,
            updated_at=now,
        ))
        self.log(issue, "created", author_id, photos=len(issue.photos))
        return issue

    def join(
        self,
        issue_id: str,
        user_id: int,
        photos: list[dict[str, str]] | None = None,
        *,
        via: str = "match",
    ) -> Issue | None:
        issue = self.repository.get(issue_id)
        if issue is None:
            return None
        if user_id in issue.participants:
            return issue
        issue.participants.add(user_id)
        if photos:
            issue.photos.extend(photos)
            issue.photo_note = "image"
        issue.updated_at = utcnow()
        self.repository.save(issue)
        self.log(issue, "joined", user_id, via=via)
        return issue

    def mass_notify_recipients(self, issue: Issue) -> list[int]:
        """Соседи, которым стоит сообщить о новой массовой проблеме.

        Подъездная категория — только жители этого подъезда, домовая — весь дом.
        Не больше одного оповещения на заявку и MASS_NOTIFY_DAILY_LIMIT в день.
        """
        rule = category_rule(issue.category)
        if rule.scope is Scope.NONE:
            return []
        entrance = issue.entrance if rule.scope is Scope.ENTRANCE else None
        today = utcnow().astimezone(DISPLAY_TZ).strftime("%Y-%m-%d")
        recipients = []
        for resident in self.directory.residents_in_scope(issue.house_id, entrance):
            if resident.user_id in issue.participants or not resident.notify_mass:
                continue
            count = resident.mass_count if resident.mass_day == today else 0
            if count >= MASS_NOTIFY_DAILY_LIMIT:
                continue
            resident.mass_day, resident.mass_count = today, count + 1
            self.directory.save_resident(resident)
            recipients.append(resident.user_id)
        if recipients:
            self.log(issue, "mass_notified", None, recipients=len(recipients))
        return recipients

    # работа УК

    def take_in_work(
        self,
        issue_id: str,
        actor_id: int | None = None,
        *,
        planned_at: datetime | None = None,
    ) -> Issue | None:
        issue = self.repository.get(issue_id)
        if issue is None or issue.status not in {IssueStatus.OPEN, IssueStatus.DISPUTED}:
            return None
        now = utcnow()
        issue.status = IssueStatus.IN_PROGRESS
        issue.taken_at = issue.taken_at or now
        if planned_at is not None:
            issue.planned_at = planned_at
        issue.updated_at = now
        self.repository.save(issue)
        self.log(issue, "taken", actor_id)
        return issue

    def set_plan(
        self,
        issue_id: str,
        actor_id: int | None,
        *,
        planned_at: datetime | None = None,
        comment: str | None = None,
    ) -> Issue | None:
        issue = self.repository.get(issue_id)
        if issue is None or issue.status not in ACTIVE_STATUSES:
            return None
        if planned_at is not None:
            issue.planned_at = planned_at
        if comment is not None:
            issue.uk_comment = comment.strip()[:500]
        issue.updated_at = utcnow()
        self.repository.save(issue)
        self.log(issue, "planned", actor_id,
                 planned_at=planned_at.isoformat() if planned_at else None, comment=comment)
        return issue

    def assign(self, issue_id: str, assignee_id: int, actor_id: int | None) -> Issue | None:
        issue = self.repository.get(issue_id)
        if issue is None or issue.status not in ACTIVE_STATUSES:
            return None
        if issue.status is not IssueStatus.IN_PROGRESS:
            issue.status = IssueStatus.IN_PROGRESS
            issue.taken_at = issue.taken_at or utcnow()
        issue.assignee_id = assignee_id
        issue.updated_at = utcnow()
        self.repository.save(issue)
        self.log(issue, "assigned", actor_id, assignee_id=assignee_id)
        return issue

    def mark_completed_by_uk(
        self,
        issue_id: str,
        actor_id: int | None = None,
        result_photos: list[dict[str, str]] | None = None,
    ) -> Issue | None:
        issue = self.repository.get(issue_id)
        if issue is None or issue.status not in ACTIVE_STATUSES:
            return None
        now = utcnow()
        issue.status = IssueStatus.WAITING_CONFIRMATION
        issue.confirmations.clear()
        issue.disputes.clear()
        issue.reminder_sent = False
        issue.taken_at = issue.taken_at or now
        issue.completed_at = now
        if result_photos:
            issue.result_photos = list(result_photos)
        issue.updated_at = now
        self.repository.save(issue)
        self.log(issue, "completed", actor_id)
        return issue

    def add_result_photos(self, issue_id: str, photos: list[dict[str, str]]) -> Issue | None:
        issue = self.repository.get(issue_id)
        if issue is None or issue.status is not IssueStatus.WAITING_CONFIRMATION:
            return None
        issue.result_photos.extend(photos)
        return self.repository.save(issue)

    # подтверждение жителями

    def can_vote(self, issue: Issue, user_id: int) -> bool:
        return user_id in issue.participants or self.directory.is_chairman(user_id, issue.house_id)

    def vote(self, issue_id: str, user_id: int, approve: bool) -> tuple[Issue | None, str | None]:
        """Голос жителя. Правило:

        * председатель совета дома решает сразу;
        * иначе итог наступает, когда большинство всех участников ответило
          одинаково или ответили все; при равенстве — «оспорено» (пусть УК проверит);
        * оставшееся решает тайм-аут: большинство ответивших или «нет ответа».
        """
        issue = self.repository.get(issue_id)
        if issue is None or not self.can_vote(issue, user_id):
            return None, None
        if issue.status not in {IssueStatus.WAITING_CONFIRMATION, IssueStatus.DONE}:
            return None, None
        target, other = (
            (issue.confirmations, issue.disputes) if approve else (issue.disputes, issue.confirmations)
        )
        target.add(user_id)
        other.discard(user_id)
        issue.updated_at = utcnow()
        self.log(issue, "vote", user_id, approve=approve)

        if self.directory.is_chairman(user_id, issue.house_id):
            decision = IssueStatus.CONFIRMED if approve else IssueStatus.DISPUTED
        else:
            decision = self._decide(issue, final=False)
        if decision is None:
            self.repository.save(issue)
            return issue, VoteResult.RECORDED
        self._close_round(issue, decision, actor_id=user_id)
        return issue, (VoteResult.CONFIRMED if decision is IssueStatus.CONFIRMED else VoteResult.DISPUTED)

    def confirm(self, issue_id: str, user_id: int) -> Issue | None:
        return self.vote(issue_id, user_id, True)[0]

    def dispute(self, issue_id: str, user_id: int) -> Issue | None:
        return self.vote(issue_id, user_id, False)[0]

    @staticmethod
    def _decide(issue: Issue, *, final: bool) -> IssueStatus | None:
        voters = len(issue.participants)
        yes, no = len(issue.confirmations), len(issue.disputes)
        if yes * 2 > voters:
            return IssueStatus.CONFIRMED
        if no * 2 > voters:
            return IssueStatus.DISPUTED
        if yes + no >= voters or (final and yes + no > 0):
            return IssueStatus.CONFIRMED if yes > no else IssueStatus.DISPUTED
        if final:
            return IssueStatus.COMPLETED_NO_RESPONSE
        return None

    def _close_round(self, issue: Issue, decision: IssueStatus, actor_id: int | None) -> None:
        now = utcnow()
        issue.status = decision
        issue.updated_at = now
        if decision is IssueStatus.CONFIRMED:
            issue.confirmed_at = now
            self.log(issue, "confirmed", actor_id)
        elif decision is IssueStatus.DISPUTED:
            issue.dispute_count += 1
            issue.completed_at = None
            self.log(issue, "disputed", actor_id, round=issue.dispute_count)
        else:
            self.log(issue, "no_response", actor_id)
        self.repository.save(issue)

    def set_status(self, issue_id: str, status: IssueStatus) -> Issue | None:
        issue = self.repository.get(issue_id)
        if issue is None:
            return None
        issue.status = status
        issue.updated_at = utcnow()
        return self.repository.save(issue)

    def expire_waiting(self, timeout: timedelta) -> list[Issue]:
        """По истечении срока решает большинство ответивших; если ответов нет — «нет ответа»."""
        now = utcnow()
        closed: list[Issue] = []
        for issue in self.repository.list_waiting_confirmation():
            started = issue.completed_at or issue.updated_at
            if started + timeout <= now:
                self._close_round(issue, self._decide(issue, final=True), actor_id=None)
                closed.append(issue)
        return closed

    def due_reminders(self, timeout: timedelta) -> list[tuple[Issue, list[int]]]:
        """На середине срока ожидания — кому из участников напомнить."""
        now = utcnow()
        due = []
        for issue in self.repository.list_waiting_confirmation():
            started = issue.completed_at or issue.updated_at
            if issue.reminder_sent or started + timeout / 2 > now:
                continue
            silent = sorted(issue.participants - issue.confirmations - issue.disputes)
            issue.reminder_sent = True
            self.repository.save(issue)
            if silent:
                self.log(issue, "reminded", None, recipients=len(silent))
                due.append((issue, silent))
        return due

    def collect_newly_overdue(self, now: datetime | None = None) -> list[Issue]:
        """Заявки, у которых только что истёк срок устранения; каждая — один раз."""
        now = now or utcnow()
        overdue: list[Issue] = []
        for issue in self.repository.list_for_uk(set(ACTIVE_STATUSES)):
            if not issue.overdue_notified and is_overdue(issue, now):
                issue.overdue_notified = True
                self.repository.save(issue)
                self.log(issue, "overdue", None)
                overdue.append(issue)
        return overdue

    # выборки

    def issues(self, org_id: str | None = None, statuses: set[IssueStatus] | None = None) -> list[Issue]:
        issues = self.repository.list_for_uk(statuses)
        if org_id:
            issues = [issue for issue in issues if issue.org_id == org_id]
        return issues

    def queue(
        self,
        org_id: str | None = None,
        statuses: set[IssueStatus] | None = None,
        now: datetime | None = None,
    ) -> list[Issue]:
        """Очередь УК: сначала самые приоритетные."""
        now = now or utcnow()
        issues = self.issues(org_id, statuses)
        return sorted(issues, key=lambda issue: priority_score(issue, now), reverse=True)

    def house_issues(self, house_id: str, *, active_only: bool = True) -> list[Issue]:
        statuses = set(ACTIVE_STATUSES) | {IssueStatus.WAITING_CONFIRMATION} if active_only else None
        issues = [issue for issue in self.repository.list_for_uk(statuses) if issue.house_id == house_id]
        return sorted(issues, key=lambda issue: priority_score(issue), reverse=True)

    def assigned_to(self, user_id: int) -> list[Issue]:
        return [
            issue for issue in self.repository.list_for_uk(set(ACTIVE_STATUSES))
            if issue.assignee_id == user_id
        ]

    def can_view(self, issue: Issue, user_id: int) -> bool:
        if user_id in issue.participants:
            return True
        staff = self.directory.staff(user_id)
        if staff is not None and staff.org_id == issue.org_id:
            return True
        resident = self.directory.resident(user_id)
        return resident.house_id == issue.house_id or self.directory.is_chairman(user_id, issue.house_id)

    # объявления

    def announce(self, house: House, text: str, author_id: int) -> tuple[Announcement, list[int]]:
        recipients = [resident.user_id for resident in self.directory.residents_in_scope(house.id)]
        announcement = self.repository.put(Announcement(
            house_id=house.id, text=text.strip()[:2000], author_id=author_id,
            recipients=len(recipients),
        ))
        return announcement, recipients

    def announcements(self, house_id: str, limit: int = 10) -> list[Announcement]:
        items = self.repository.select(Announcement, house_id=house_id)
        return sorted(items, key=lambda item: item.created_at, reverse=True)[:limit]

    # метрики пилота

    def stats(
        self,
        org_id: str | None = None,
        house_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Метрики из описания пилота плюс оперативная сводка.

        1. доля дублей среди обращений жителей;
        2. повторные обращения по той же проблеме после её закрытия (30 дней);
        3. доля выполнений, подтверждённых жителями;
        4. доля выполнений, оспоренных жителями;
        5. среднее время от подачи до подтверждённого устранения.
        """
        now = now or utcnow()
        issues = self.issues(org_id)
        if house_id:
            issues = [issue for issue in issues if issue.house_id == house_id]
        issue_ids = {issue.id for issue in issues}
        events = [event for event in self.events() if event.issue_id in issue_ids]

        created = sum(1 for event in events if event.kind == "created")
        joins = [event for event in events if event.kind == "joined"]
        reports = created + len(joins)
        outcomes = {
            kind: sum(1 for event in events if event.kind == kind)
            for kind in ("confirmed", "disputed", "no_response")
        }
        rounds = sum(outcomes.values())

        by_status = {status.value: 0 for status in IssueStatus}
        by_category: dict[str, int] = {}
        houses: dict[str, dict[str, Any]] = {}
        reaction, resolution, to_confirmed = [], [], []
        for issue in issues:
            by_status[issue.status.value] += 1
            by_category[issue.category] = by_category.get(issue.category, 0) + 1
            house = houses.setdefault(issue.house_id or issue.house_key, {
                "house_id": issue.house_id, "house": issue.house,
                "total": 0, "active": 0, "overdue": 0,
            })
            house["total"] += 1
            house["active"] += issue.status in ACTIVE_STATUSES
            house["overdue"] += is_overdue(issue, now)
            if issue.taken_at:
                reaction.append((issue.taken_at - issue.created_at).total_seconds() / 3600)
            if issue.completed_at:
                resolution.append((issue.completed_at - issue.created_at).total_seconds() / 3600)
            if issue.confirmed_at:
                to_confirmed.append((issue.confirmed_at - issue.created_at).total_seconds() / 3600)

        def average(values: list[float]) -> float | None:
            return round(sum(values) / len(values), 1) if values else None

        def share(part: int, whole: int) -> int | None:
            return round(100 * part / whole) if whole else None

        return {
            "total_issues": len(issues),
            "total_reports": reports,
            "duplicates_merged": len(joins),
            "duplicate_share": share(len(joins), reports),
            "joined_from_alerts": sum(1 for event in joins if event.data.get("via") == "alert"),
            "repeat_issues": self._repeat_issues(issues),
            "completion_rounds": rounds,
            "confirmed_share": share(outcomes["confirmed"], rounds),
            "disputed_share": share(outcomes["disputed"], rounds),
            "no_response_share": share(outcomes["no_response"], rounds),
            "avg_to_confirmed_hours": average(to_confirmed),
            "avg_reaction_hours": average(reaction),
            "avg_resolution_hours": average(resolution),
            "active": sum(1 for issue in issues if issue.status in ACTIVE_STATUSES),
            "waiting_confirmation": by_status[IssueStatus.WAITING_CONFIRMATION],
            "overdue": sum(1 for issue in issues if is_overdue(issue, now)),
            "residents": len(set().union(*(issue.participants for issue in issues))) if issues else 0,
            "by_status": by_status,
            "by_category": [
                {
                    "category": key,
                    "label": category_rule(key).label,
                    "emoji": category_rule(key).emoji,
                    "count": count,
                }
                for key, count in sorted(by_category.items(), key=lambda item: -item[1])
            ],
            "houses": sorted(houses.values(), key=lambda item: (-item["active"], -item["total"]))[:10],
        }

    @staticmethod
    def _repeat_issues(issues: list[Issue]) -> int:
        """Новая заявка по той же проблеме (дом, категория, подъезд) в течение
        30 дней после того, как прошлую закрыли, — повторное обращение."""
        finished = [issue for issue in issues if issue.status in FINISHED_STATUSES]
        repeats = 0
        for issue in issues:
            rule = category_rule(issue.category)
            for earlier in finished:
                closed_at = earlier.confirmed_at or earlier.completed_at or earlier.updated_at
                same_place = earlier.house_id == issue.house_id and earlier.category == issue.category
                if rule.scope is Scope.ENTRANCE:
                    same_place = same_place and earlier.entrance == issue.entrance
                if (
                    earlier.id != issue.id and same_place
                    and closed_at <= issue.created_at <= closed_at + REPEAT_WINDOW
                ):
                    repeats += 1
                    break
        return repeats


# ---------------------------------------------------------------- представление


def format_issue(issue: Issue, now: datetime | None = None, *, for_staff: bool = True) -> str:
    """Карточка заявки. Жителю — без служебных строк (норматив и приоритет видит только УК)."""
    now = now or utcnow()
    rule = category_rule(issue.category)
    lines = [
        f"{rule.emoji} {rule.label} · заявка #{issue.id}",
        f"🏠 {issue.house}, подъезд {issue.entrance}",
        f"Статус: {STATUS_LABELS[issue.status]}",
        f"Жителей: {len(issue.participants)}",
    ]
    if issue.status is IssueStatus.WAITING_CONFIRMATION:
        lines.append(
            f"Ответили: ✅ {len(issue.confirmations)} · ❌ {len(issue.disputes)} "
            f"из {len(issue.participants)}"
        )
    if issue.status in ACTIVE_STATUSES:
        if issue.planned_at:
            lines.append(f"📅 Плановый срок: до {format_day(issue.planned_at)}")
        if for_staff:
            deadline = f"Норматив: до {format_local(sla_deadline(issue))}"
            if is_overdue(issue, now):
                deadline += " — ⏰ просрочено"
            lines.append(deadline)
            lines.append(f"Приоритет: {priority_label(issue, now)}")
    if issue.uk_comment:
        lines.append(f"💬 УК: {issue.uk_comment}")
    if issue.dispute_count and issue.status in ACTIVE_STATUSES:
        lines.append(f"⚠️ Жители оспаривали выполнение: {issue.dispute_count} раз")
    if issue.photos or issue.photo_note:
        lines.append(f"Фото жителей: {len(issue.photos) or 1}")
    if issue.result_photos:
        lines.append(f"Фото результата: {len(issue.result_photos)}")
    lines.append(f"Описание: {issue.description}")
    return "\n".join(lines)


def official_appeal_text(issue: Issue, now: datetime | None = None) -> str:
    """Текст для официального обращения, если УК нарушила срок или жители оспорили выполнение."""
    now = now or utcnow()
    rule = category_rule(issue.category)
    reason = (
        f"Жители оспорили выполнение {issue.dispute_count} раз."
        if issue.dispute_count
        else f"Нормативный срок устранения истёк {format_local(sla_deadline(issue))}."
    )
    return (
        f"Прошу устранить общедомовую неисправность: {rule.label.lower()} — "
        f"{issue.description}. Адрес: {issue.house}, подъезд {issue.entrance}. "
        f"Проблема зарегистрирована {format_local(issue.created_at)}, "
        f"затронуто жителей: {len(issue.participants)}. {reason} "
        f"Номер общей заявки: #{issue.id}."
    )

