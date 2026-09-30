"""Сценарии жителя: согласие, выбор дома, обращение, присоединение, подтверждение."""
from __future__ import annotations

from typing import Any

from ..domain import (
    ACTIVE_STATUSES,
    CATEGORIES,
    ROLE_LABELS,
    STATUS_LABELS,
    House,
    Issue,
    IssueStatus,
    Role,
    Scope,
    VoteResult,
    category_rule,
)
from . import keyboards as kb
from .core import BotCore, Draft, Reply
from .keyboards import Keyboard, Photo

WELCOME = (
    "👋 Здесь соседи сообщают о проблемах дома: лифт, свет, протечки, двор.\n"
    "Одна проблема — одна общая заявка: присоединяйтесь, а не дублируйте.\n"
    "УК отвечает всем сразу, а вы подтверждаете, что всё исправлено."
)
CONSENT = (
    "Чтобы бот работал, он хранит ваш id в MAX, выбранный дом и подъезд, ваши "
    "заявки и фото. Их видят ваша управляющая компания и соседи по заявке — без имён."
)
CONSENT_DETAILS = (
    "Какие данные хранятся\n\n"
    "• id пользователя MAX — чтобы присылать уведомления о ваших заявках;\n"
    "• дом и подъезд — чтобы находить общие заявки и сообщать о проблемах соседей;\n"
    "• текст и фото ваших обращений — их видит управляющая компания.\n\n"
    "Имя и телефон бот не сохраняет. Оповещения о проблемах соседей можно "
    "выключить в «⚙️ Мой дом». Бот не заменяет официальные каналы обращений."
)
ACTIVE_OR_WAITING = set(ACTIVE_STATUSES) | {IssueStatus.WAITING_CONFIRMATION}


class ResidentMixin(BotCore):
    # ------------------------------------------------------------------ старт и согласие

    def on_start(self, user_id: int, payload: str, name: str = "") -> str:
        self.drafts.pop(user_id, None)
        if payload.startswith("staff_"):
            return self.accept_staff_invite(user_id, payload[len("staff_"):], name)
        if not self.has_access(user_id):
            return self.consent_prompt(user_id, payload)
        return self.apply_start_payload(user_id, payload)

    def consent_prompt(self, user_id: int, payload: str) -> str:
        current = self.drafts.get(user_id)
        keep = payload or (current.payload if current else "")
        self.drafts[user_id] = Draft(step="on_consent", payload=keep)
        self._set_keyboard(user_id, kb.keyboard([
            [kb.button("✅ Согласен, продолжить", "on:consent")],
            [kb.button("ℹ️ Какие данные хранятся", "on:about")],
        ]))
        return f"{WELCOME}\n\n{CONSENT}"

    def onboarding_action(self, user_id: int, payload: str) -> Reply:
        if payload == "on:about":
            self._set_keyboard(user_id, kb.keyboard([[kb.button("✅ Согласен, продолжить", "on:consent")]]))
            return CONSENT_DETAILS, None
        draft = self.drafts.pop(user_id, None)
        self.directory.give_consent(user_id)
        return self.apply_start_payload(user_id, draft.payload if draft else ""), None

    def accept_staff_invite(self, user_id: int, code: str, name: str) -> str:
        invite = self.directory.accept_invite(code, user_id, name)
        self._set_keyboard(user_id, self.main_keyboard(user_id))
        if invite is None:
            return "Приглашение уже использовано или устарело. Попросите администратора УК прислать новое."
        org = self.directory.get_org(invite.org_id)
        org_name = org.name if org else "УК"
        if invite.role == "chairman":
            house = self.directory.get_house(invite.house_id or "")
            return (
                f"✅ Вы — председатель совета дома {house.address if house else ''}.\n\n"
                "Ваш ответ «устранена / осталась» решает итог по заявкам дома, а в меню "
                "появилась «🏘 Сводка по дому»."
            )
        return (
            f"✅ Вы добавлены в {org_name} как {ROLE_LABELS[Role(invite.role)]}.\n\n"
            + self.home_text(user_id)
        )

    def apply_start_payload(self, user_id: int, payload: str) -> str:
        if payload.startswith("h_"):
            house_id, _, entrance = payload[2:].rpartition("_")
            house = self.directory.get_house(house_id)
            if house is not None:
                return self.finish_house_choice(user_id, house, entrance or "1", next_action="home")
        if payload.startswith("join_"):
            issue = self.repository.get(payload[len("join_"):])
            if issue is not None and issue.status in ACTIVE_OR_WAITING:
                return self.invitation_view(user_id, issue)
        if self.directory.resident(user_id).house_id is None and self.staff(user_id) is None:
            return self.start_house_selection(user_id, "home")
        self._set_keyboard(user_id, self.main_keyboard(user_id))
        return self.home_text(user_id)

    def invitation_view(self, user_id: int, issue: Issue) -> str:
        if user_id in issue.participants:
            self._set_keyboard(user_id, self.issue_keyboard(user_id, issue))
            return "Вы уже участник этой заявки.\n\n" + self.card(user_id, issue)
        self._set_keyboard(user_id, kb.with_photos(issue.photos, kb.keyboard([
            [kb.button("🙋 У меня тоже", f"iss:j:{issue.id}:link")],
            kb.menu_row(),
        ])))
        return (
            "Соседи зовут вас присоединиться к общей заявке:\n\n"
            + self.card(user_id, issue)
            + "\n\nЕсли проблема касается и вас, нажмите «У меня тоже» — УК увидит, "
            "что жителей больше, а вы получите уведомление, когда всё починят."
        )

    # ------------------------------------------------------------------ главное меню

    def home_text(self, user_id: int) -> str:
        resident = self.directory.resident(user_id)
        house = self.directory.get_house(resident.house_id) if resident.house_id else None
        lines: list[str] = []
        if house is not None:
            issues = self.issue_service.house_issues(house.id)
            mine = sum(1 for issue in issues if user_id in issue.participants)
            lines.append(f"🏠 {house.address}, подъезд {resident.entrance}")
            if issues:
                lines.append(f"Открытых проблем в доме: {len(issues)} · с вашим участием: {mine}")
                for issue in issues[:3]:
                    rule = category_rule(issue.category)
                    lines.append(
                        f"• {rule.emoji} {rule.label} · п. {issue.entrance} · 👥 {len(issue.participants)}"
                        f" · {STATUS_LABELS[issue.status]}"
                    )
            else:
                lines.append("В доме нет открытых проблем 🎉")
        else:
            lines.append(
                "Выберите свой дом, чтобы сообщать о проблемах и видеть, о чём уже "
                "сообщили соседи."
            )
        staff = self.staff(user_id)
        if staff is not None:
            org = self.directory.get_org(staff.org_id)
            lines.append(f"\n👷 Вы — {ROLE_LABELS[staff.role]}, {org.name if org else 'УК'}.")
        for chaired in self.directory.chaired_houses(user_id):
            lines.append(f"🏘 Вы — председатель совета дома {chaired.address}.")
        return "\n".join(lines)

    def main_keyboard(self, user_id: int) -> Keyboard:
        resident = self.directory.resident(user_id)
        house_count = len(self.issue_service.house_issues(resident.house_id)) if resident.house_id else 0
        rows = [
            [kb.button("➕ Сообщить о проблеме", "menu:new")],
            [
                kb.button(f"🏘 Проблемы дома ({house_count})", "menu:house"),
                kb.button("📋 Мои заявки", "menu:mine"),
            ],
            [
                kb.button("⚙️ Мой дом", "menu:settings"),
                kb.button("ℹ️ Как это работает", "menu:help"),
            ],
        ]
        staff = self.staff(user_id)
        if staff is not None and staff.role in {Role.ADMIN, Role.DISPATCHER}:
            rows.append([
                kb.button("🏢 Очередь заявок", "menu:uk"),
                kb.button("📢 Объявление", "uk:announce"),
            ])
        if staff is not None and staff.role is Role.MASTER:
            rows.append([kb.button("🧰 Мои задачи", "mst:tasks")])
        if staff is not None and staff.role is Role.ADMIN:
            rows.append([kb.button("🛠 Управление УК", "adm:menu")])
        if self.directory.chaired_houses(user_id):
            rows.append([kb.button("🏘 Сводка по дому", "chair:summary")])
        return kb.keyboard(rows)

    def menu_action(self, user_id: int, payload: str) -> Reply:
        parts = payload.split(":")
        section = parts[1] if len(parts) > 1 else ""
        page = self.to_int(parts[2]) if len(parts) > 2 else 0
        self.drafts.pop(user_id, None)
        if section == "new":
            return self.start_report(user_id), None
        if section == "mine":
            return self.my_issues_view(user_id, page), None
        if section == "house":
            return self.house_issues_view(user_id, page), None
        if section == "settings":
            return self.settings_view(user_id), None
        if section == "uk":
            return self.staff_action(user_id, "uk:list:active:0")
        return self.home_text(user_id), self.main_keyboard(user_id)

    # ------------------------------------------------------------------ выбор дома

    def start_house_selection(self, user_id: int, next_action: str, intro: str = "") -> str:
        self.draft(user_id, "res_house", next=next_action)
        houses = self.directory.list_houses()
        shown = houses if len(houses) <= 8 else []
        self._set_keyboard(user_id, kb.house_choice_keyboard(shown, "home:h:"))
        ways = [
            "• отправьте геолокацию — покажу ближайшие дома (в приложении MAX; в веб-версии напишите адрес);",
            "• или напишите адрес, например «Ленина 10»;",
            "• или отсканируйте QR-код в подъезде.",
        ]
        if shown:
            ways.insert(0, "• выберите адрес в списке;")
        return (intro + "\n\n" if intro else "") + "🏠 Где ваш дом?\n\n" + "\n".join(ways)

    def choose_house_by_location(self, user_id: int, location: tuple[float, float], next_action: str) -> str:
        self.draft(user_id, "res_house", next=next_action)
        nearest = self.directory.nearest_houses(*location)
        if not nearest:
            self._set_keyboard(user_id, kb.house_choice_keyboard([], "home:h:"))
            return (
                "Рядом нет домов, которые обслуживаются в сервисе. Напишите адрес "
                "или отсканируйте QR-код в подъезде."
            )
        houses = [house for house, _ in nearest]
        distances = {house.id: meters for house, meters in nearest}
        self._set_keyboard(user_id, kb.house_choice_keyboard(houses, "home:h:", distances))
        return "📍 Ближайшие дома. Выберите свой:"

    def house_selection_action(self, user_id: int, payload: str) -> Reply:
        draft = self.drafts.get(user_id) or self.draft(user_id, "res_house", next="home")
        if payload.startswith("home:h:"):
            house = self.directory.get_house(payload[len("home:h:"):])
            if house is None:
                return "Дом не найден. Выберите из списка.", None
            if house.entrances <= 1:
                return self.finish_house_choice(user_id, house, "1", draft.next), None
            self.draft(user_id, "res_entrance", house_id=house.id)
            self._set_keyboard(user_id, kb.entrance_keyboard(house, "home:e:"))
            return f"🏠 {house.address}\nКакой у вас подъезд?", None
        if payload.startswith("home:e:"):
            house = self.directory.get_house(draft.house_id or "")
            if house is None:
                return self.start_house_selection(user_id, draft.next or "home"), None
            return self.finish_house_choice(user_id, house, payload[len("home:e:"):], draft.next), None
        return self.start_house_selection(user_id, draft.next or "home"), None

    def finish_house_choice(self, user_id: int, house: House, entrance: str, next_action: str) -> str:
        entrance = str(min(max(self.to_int(entrance), 1), house.entrances))
        self.directory.set_resident_house(user_id, house.id, entrance)
        self.drafts.pop(user_id, None)
        saved = f"✅ Ваш дом: {house.address}, подъезд {entrance}."
        if next_action == "report":
            return saved + "\n\n" + self.start_report(user_id)
        if next_action == "house":
            return self.house_issues_view(user_id)
        if next_action == "settings":
            return saved + "\n\n" + self.settings_view(user_id)
        self._set_keyboard(user_id, self.main_keyboard(user_id))
        return saved + "\n\n" + self.home_text(user_id)

    # ------------------------------------------------------------------ обращение

    def start_report(self, user_id: int) -> str:
        resident = self.directory.resident(user_id)
        house = self.directory.get_house(resident.house_id) if resident.house_id else None
        if house is None:
            return self.start_house_selection(user_id, "report", "Сначала выберем ваш дом.")
        self.drafts[user_id] = Draft(step="res_category", house_id=house.id, entrance=resident.entrance)
        self._set_keyboard(user_id, kb.category_keyboard(house, resident.entrance))
        return f"Что случилось?\n🏠 {house.address}, подъезд {resident.entrance}"

    def report_action(self, user_id: int, payload: str) -> Reply:
        draft = self.drafts.get(user_id)
        if payload == "rep:house":
            return self.start_house_selection(user_id, "report"), None
        if draft is None or not draft.house_id:
            return "Сценарий устарел. Нажмите «Сообщить о проблеме».", self.main_keyboard(user_id)
        if payload.startswith("rep:cat:"):
            return self.choose_category(user_id, draft, payload[len("rep:cat:"):]), None
        if payload == "rep:join":
            return self.join_similar(user_id, draft), None
        if payload == "rep:new":
            return self.ask_description(user_id, draft), None
        return "Действие устарело. Откройте меню заново.", self.main_keyboard(user_id)

    def choose_category(self, user_id: int, draft: Draft, category: str) -> str:
        rule = category_rule(category)
        draft.category = rule.key
        similar = self.issue_service.find_similar(draft.house_id or "", draft.entrance or "1", rule.key)
        if similar is not None and user_id in similar.participants:
            self.drafts.pop(user_id, None)
            self._set_keyboard(user_id, self.issue_keyboard(user_id, similar))
            return "Вы уже участвуете в заявке по этой проблеме:\n\n" + self.card(user_id, similar)
        if similar is not None:
            draft.step = "res_match"
            draft.similar_issue_id = similar.id
            self._set_keyboard(user_id, kb.with_photos(similar.photos, kb.keyboard([
                [kb.button("🙋 У меня тоже", "rep:join")],
                [kb.button("Это другая проблема", "rep:new")],
                kb.cancel_row(),
            ])))
            return (
                f"👥 Соседи уже сообщили об этом — жителей в заявке: {len(similar.participants)}.\n\n"
                + self.card(user_id, similar)
                + "\n\nЕсли это та же проблема, нажмите «У меня тоже». Описание писать не "
                "нужно: вы получите все уведомления, а УК увидит, что затронуто больше соседей."
            )
        return self.ask_description(user_id, draft)

    def ask_description(self, user_id: int, draft: Draft) -> str:
        draft.step = "res_desc"
        self._set_keyboard(user_id, kb.keyboard([kb.cancel_row()]))
        rule = category_rule(draft.category)
        return (
            f"{rule.emoji} {rule.label}\n"
            "Опишите коротко, что случилось и где (этаж, место). К сообщению можно "
            "приложить фото — или отправить фото отдельно перед описанием."
        )

    def join_similar(self, user_id: int, draft: Draft) -> str:
        issue = self.issue_service.join(draft.similar_issue_id or "", user_id, draft.photos, via="match")
        self.drafts.pop(user_id, None)
        if issue is None:
            self._set_keyboard(user_id, self.main_keyboard(user_id))
            return "Заявка больше недоступна. Создайте новую через меню."
        self.after_join(user_id, issue, with_photos=bool(draft.photos))
        self._set_keyboard(user_id, self.issue_keyboard(user_id, issue))
        return "✅ Вы присоединились к общей заявке:\n\n" + self.card(user_id, issue)

    def after_join(self, user_id: int, issue: Issue, *, with_photos: bool = False) -> None:
        self.notify_staff(
            user_id, issue,
            f"👥 К заявке присоединился житель. Теперь жителей: {len(issue.participants)}",
            with_photos=with_photos,
        )

    def create_issue_from_draft(self, user_id: int, draft: Draft) -> str:
        house = self.directory.get_house(draft.house_id or "")
        self.drafts.pop(user_id, None)
        if house is None:
            return self.start_house_selection(user_id, "report", "Дом не найден, выберите заново.")
        issue = self.issue_service.create(
            house=house, entrance=draft.entrance or "1", category=draft.category,
            description=draft.description, author_id=user_id, photos=draft.photos,
        )
        self.notify_staff(user_id, issue, "🆕 Новая заявка", with_photos=True)
        alerted = self.alert_neighbors(user_id, issue)
        self._set_keyboard(user_id, self.issue_keyboard(user_id, issue, include_menu=True))
        reply = "✅ Заявка создана\n\n" + self.card(user_id, issue)
        if alerted:
            reply += f"\n\n📣 Оповестили соседей: {alerted}. Они смогут присоединиться одной кнопкой."
        return reply

    def alert_neighbors(self, actor_id: int, issue: Issue) -> int:
        """Массовое оповещение: соседи присоединяются одной кнопкой, без описания."""
        rule = category_rule(issue.category)
        if rule.scope is Scope.NONE:
            return 0
        where = f"подъезд {issue.entrance}" if rule.scope is Scope.ENTRANCE else "весь дом"
        text = (
            f"⚠️ Соседи сообщили о проблеме ({where})\n\n"
            f"{rule.emoji} {rule.label}: {issue.description}\n"
            f"🏠 {issue.house}, подъезд {issue.entrance}\n\n"
            "Если это касается и вас — нажмите «У меня тоже». УК увидит масштаб, а вы "
            "получите уведомление, когда всё починят."
        )
        recipients = self.issue_service.mass_notify_recipients(issue)
        keyboard = kb.keyboard([
            [kb.button("🙋 У меня тоже", f"iss:j:{issue.id}:alert"), kb.button("Не касается", f"iss:skip:{issue.id}")],
            [kb.button("🔕 Не присылать такие оповещения", "set:mute")],
        ])
        for recipient in recipients:
            self.to_user(actor_id, recipient, text, keyboard)
        self.post_to_house_chat(
            actor_id, issue,
            f"⚠️ Соседи сообщили о проблеме ({where}): {rule.label.lower()} — {issue.description}\n"
            f"🏠 {issue.house}, подъезд {issue.entrance} · жителей: {len(issue.participants)}\n\n"
            "Касается и вас? Присоединяйтесь — УК ответит всем сразу.",
            join_button=True,
        )
        return len(recipients)

    def resident_step(
        self, user_id: int, draft: Draft, text: str, photos: list[Photo], location: tuple[float, float] | None
    ) -> str:
        if draft.step == "res_house":
            if location is not None:
                return self.choose_house_by_location(user_id, location, draft.next)
            if len(text) < 2:
                return self.start_house_selection(user_id, draft.next)
            found = self.directory.search_houses(text)
            self._set_keyboard(user_id, kb.house_choice_keyboard(found, "home:h:"))
            if not found:
                return (
                    "Не нашёл такой дом среди подключённых к сервису. Проверьте адрес, "
                    "отправьте геолокацию или отсканируйте QR-код в подъезде."
                )
            return "Нашёл дома. Выберите свой:"
        if draft.step == "res_entrance":
            house = self.directory.get_house(draft.house_id or "")
            if house is None or not text.isdigit():
                return "Выберите подъезд кнопкой."
            return self.finish_house_choice(user_id, house, text, draft.next)
        if draft.step == "res_category":
            match = next(
                (rule for rule in CATEGORIES
                 if text and (rule.key.startswith(text.lower()) or rule.label.lower().startswith(text.lower()))),
                None,
            )
            if match is None:
                house = self.directory.get_house(draft.house_id or "")
                self._set_keyboard(user_id, kb.category_keyboard(house, draft.entrance))
                return "Выберите категорию кнопкой."
            return self.choose_category(user_id, draft, match.key)
        if draft.step == "res_match":
            answer = text.lower()
            if answer in {"да", "д", "yes", "у меня тоже"}:
                return self.join_similar(user_id, draft)
            if answer in {"нет", "н", "no", "это другая проблема"}:
                return self.ask_description(user_id, draft)
            return "Выберите «У меня тоже» или «Это другая проблема»."
        if draft.step == "res_desc":
            if photos:
                draft.photos.extend(photos)
            if len(text) < 5:
                self._set_keyboard(user_id, kb.keyboard([kb.cancel_row()]))
                if photos and not text:
                    return f"Фото добавлено ({len(draft.photos)}). Теперь опишите проблему текстом."
                return "Добавьте немного деталей: что именно произошло и где."
            draft.description = text[:1000]
            return self.create_issue_from_draft(user_id, draft)
        self.drafts.pop(user_id, None)
        return "Начните заново через меню."

    # ------------------------------------------------------------------ карточка заявки

    def issue_action(self, user_id: int, payload: str) -> Reply:
        parts = payload.split(":")
        action, issue_id = parts[1], parts[2] if len(parts) > 2 else ""
        issue = self.repository.get(issue_id)
        if issue is None:
            return "Заявка не найдена.", self.main_keyboard(user_id)
        if action == "v":
            return self.issue_view(user_id, issue)
        if action == "j":
            return self.join_from_button(user_id, issue, via=parts[3] if len(parts) > 3 else "house")
        if action == "skip":
            self.issue_service.log(issue, "alert_skipped", user_id)
            return "Понятно, по этой заявке больше не побеспокоим.", self.main_keyboard(user_id)
        if action in {"ok", "no"}:
            return self.vote(user_id, issue, approve=action == "ok")
        return "Действие устарело.", self.main_keyboard(user_id)

    def issue_view(self, user_id: int, issue: Issue) -> Reply:
        if not self.issue_service.can_view(issue, user_id):
            return "Эта заявка доступна жителям её дома и УК.", self.main_keyboard(user_id)
        text = self.card(user_id, issue)
        if user_id in issue.participants:
            text += "\n\nВы участник этой заявки."
        photos = issue.result_photos + issue.photos
        return text, kb.with_photos(photos, self.issue_keyboard(user_id, issue))

    def join_from_button(self, user_id: int, issue: Issue, via: str) -> Reply:
        if issue.status not in ACTIVE_OR_WAITING:
            return "Заявка уже закрыта. Если проблема осталась, сообщите о ней заново.", self.main_keyboard(user_id)
        if user_id not in issue.participants:
            issue = self.issue_service.join(issue.id, user_id, via=via)
            resident = self.directory.resident(user_id)
            if resident.house_id is None:
                self.directory.set_resident_house(user_id, issue.house_id, issue.entrance)
            self.after_join(user_id, issue)
        return "✅ Вы в общей заявке — пришлём уведомление, когда УК ответит.\n\n" + self.card(user_id, issue), \
            self.issue_keyboard(user_id, issue)

    def register_vote(self, user_id: int, issue_id: str, approve: bool) -> tuple[Issue | None, str | None]:
        """Голос жителя и уведомления о нём."""
        updated, result = self.issue_service.vote(issue_id, user_id, approve)
        if updated is None:
            return None, None
        if result == VoteResult.RECORDED:
            if not approve:
                self.notify_staff(
                    user_id, updated,
                    f"⚠️ Житель считает, что проблема осталась ({len(updated.disputes)} из "
                    f"{len(updated.participants)}). Итог ещё не определён",
                )
        else:
            self.announce_outcome(user_id, updated)
        return updated, result

    def vote(self, user_id: int, issue: Issue, *, approve: bool) -> Reply:
        updated, result = self.register_vote(user_id, issue.id, approve)
        if updated is None:
            if self.issue_service.can_vote(issue, user_id):
                return "Ответить можно, когда УК отметит выполнение.", self.issue_keyboard(user_id, issue)
            return "Отвечать могут только участники заявки.", self.main_keyboard(user_id)
        chairman = self.directory.is_chairman(user_id, updated.house_id)
        if result == VoteResult.RECORDED:
            return (
                "Спасибо, ответ учтён.\n"
                f"Итог решает большинство: ✅ {len(updated.confirmations)} · ❌ {len(updated.disputes)} "
                f"из {len(updated.participants)}."
            ), self.issue_keyboard(user_id, updated)
        if result == VoteResult.CONFIRMED:
            reply = (
                "✅ Ваше решение как председателя совета дома учтено — заявка закрыта."
                if chairman else "✅ Спасибо! Жители подтвердили выполнение — заявка закрыта."
            )
        else:
            reply = "Спасибо. Заявка возвращена УК на проверку."
        return reply + "\n\n" + self.card(user_id, updated), self.issue_keyboard(user_id, updated)

    def issue_keyboard(self, user_id: int, issue: Issue, *, include_menu: bool = False) -> Keyboard:
        rows: list[list[dict[str, Any]]] = []
        participant = user_id in issue.participants
        staff = self.staff(user_id)
        queue_staff = self.is_queue_staff(user_id, issue.org_id)
        if issue.status is IssueStatus.WAITING_CONFIRMATION and self.issue_service.can_vote(issue, user_id):
            rows.append([
                kb.button("✅ Устранена", f"iss:ok:{issue.id}"),
                kb.button("❌ Проблема осталась", f"iss:no:{issue.id}"),
            ])
        resident = self.directory.resident(user_id)
        if (
            not participant and issue.status in ACTIVE_OR_WAITING and not queue_staff
            and resident.house_id == issue.house_id
        ):
            rows.append([kb.button("🙋 У меня тоже", f"iss:j:{issue.id}:house")])
        if queue_staff:
            if issue.status in {IssueStatus.OPEN, IssueStatus.DISPUTED}:
                rows.append([kb.button("🛠 Взять в работу", f"uk:take:{issue.id}")])
            if issue.status in ACTIVE_STATUSES:
                rows.append([
                    kb.button("📅 Срок и сообщение", f"uk:plan:{issue.id}"),
                    kb.button("👷 Исполнитель", f"uk:asg:{issue.id}"),
                ])
                rows.append([kb.button("✅ Отметить выполнение", f"uk:done:{issue.id}")])
            if issue.status is IssueStatus.WAITING_CONFIRMATION:
                rows.append([kb.button("📷 Фото результата", f"uk:photo:{issue.id}")])
        elif staff is not None and staff.role is Role.MASTER and issue.assignee_id == user_id:
            if issue.status in ACTIVE_STATUSES:
                rows.append([kb.button("✅ Выполнено", f"uk:done:{issue.id}")])
        if participant and issue.status in ACTIVE_STATUSES:
            rows.append([kb.clipboard(
                "📣 Позвать соседей",
                f"Соседи, в доме {issue.house} (подъезд {issue.entrance}) проблема: "
                f"{issue.description[:120]}. Если вас это тоже касается, присоединяйтесь "
                f"к общей заявке: {self.join_link(issue.id)}",
            )])
        if participant and self.needs_appeal(issue):
            rows.extend(self.appeal_buttons(issue))
        if include_menu:
            rows.append([kb.button("➕ Ещё обращение", "menu:new")])
        rows.append([
            kb.button("🏢 Очередь", "menu:uk") if queue_staff else kb.button("🏘 Проблемы дома", "menu:house"),
            kb.button("🏠 Меню", "menu:back"),
        ])
        return kb.keyboard(rows)

    # ------------------------------------------------------------------ списки

    def house_issues_view(self, user_id: int, page: int = 0) -> str:
        resident = self.directory.resident(user_id)
        house = self.directory.get_house(resident.house_id) if resident.house_id else None
        if house is None:
            return self.start_house_selection(user_id, "house")
        issues = self.issue_service.house_issues(house.id)
        page_items, page, pages = self.paginate(issues, page)
        self._set_keyboard(user_id, kb.issue_list_keyboard(page_items, page, pages, "menu:house:"))
        if not issues:
            return f"🏘 {house.address}\n\nСейчас в доме нет открытых проблем 🎉"
        lines = [
            f"{category_rule(issue.category).emoji} {category_rule(issue.category).label} · п. {issue.entrance}"
            f" · 👥 {len(issue.participants)} · {STATUS_LABELS[issue.status]}"
            + (" · вы участвуете" if user_id in issue.participants else "")
            for issue in page_items
        ]
        header = f"🏘 Проблемы дома {house.address}" + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
        return header + "\n\n" + "\n".join(lines) + "\n\nОткройте заявку, чтобы присоединиться."

    def my_issues_view(self, user_id: int, page: int = 0) -> str:
        issues = self.repository.list_for_user(user_id)
        page_items, page, pages = self.paginate(issues, page)
        self._set_keyboard(user_id, kb.issue_list_keyboard(page_items, page, pages, "menu:mine:"))
        if not issues:
            return "У вас пока нет заявок. Нажмите «➕ Сообщить о проблеме»."
        header = "📋 Мои заявки" + (f" · стр. {page + 1}/{pages}" if pages > 1 else "")
        return header + "\n\n" + "\n".join(
            f"{category_rule(item.category).emoji} {category_rule(item.category).label} · "
            f"{item.house} · {STATUS_LABELS[item.status]}"
            for item in page_items
        )

    # ------------------------------------------------------------------ настройки

    def settings_view(self, user_id: int) -> str:
        resident = self.directory.resident(user_id)
        house = self.directory.get_house(resident.house_id) if resident.house_id else None
        self._set_keyboard(user_id, kb.keyboard([
            [kb.button("🏠 Сменить дом или подъезд", "set:house")],
            [kb.button("🔕 Выключить оповещения", "set:mute") if resident.notify_mass
             else kb.button("🔔 Включить оповещения", "set:unmute")],
            kb.menu_row(),
        ]))
        place = f"{house.address}, подъезд {resident.entrance}" if house else "не выбран"
        state = "включены" if resident.notify_mass else "выключены"
        return (
            f"⚙️ Мой дом\n\n🏠 {place}\n"
            f"🔔 Оповещения о проблемах соседей: {state}\n\n"
            "Оповещения приходят, когда соседи сообщают о проблеме вашего подъезда "
            "или всего дома, — не больше трёх в день."
        )

    def settings_action(self, user_id: int, payload: str) -> Reply:
        if payload == "set:house":
            return self.start_house_selection(user_id, "settings"), None
        if payload in {"set:mute", "set:unmute"}:
            resident = self.directory.resident(user_id)
            resident.notify_mass = payload == "set:unmute"
            self.directory.save_resident(resident)
            return self.settings_view(user_id), None
        return self.settings_view(user_id), None

    # ------------------------------------------------------------------ председатель

    def chairman_action(self, user_id: int, payload: str) -> Reply:
        houses = self.directory.chaired_houses(user_id)
        if not houses:
            return "Раздел доступен председателю совета дома.", self.main_keyboard(user_id)
        blocks = []
        issues: list[Issue] = []
        for house in houses:
            stats = self.issue_service.stats(house_id=house.id)
            house_issues = self.issue_service.house_issues(house.id)
            issues.extend(house_issues)
            blocks.append(
                f"🏘 {house.address}\n"
                f"Открыто: {stats['active']} · ждут подтверждения: {stats['waiting_confirmation']} · "
                f"просрочено: {stats['overdue']}\n"
                f"Подтверждено жителями: {self.percent(stats['confirmed_share'])} · "
                f"оспорено: {self.percent(stats['disputed_share'])}"
            )
        page_items, page, pages = self.paginate(issues, 0)
        return (
            "\n\n".join(blocks) + "\n\nВаш ответ «устранена / осталась» по заявкам дома — решающий."
        ), kb.issue_list_keyboard(page_items, page, pages, "chair:summary:")

    @staticmethod
    def percent(value: int | None) -> str:
        return "—" if value is None else f"{value}%"

    # ------------------------------------------------------------------ текстовые команды

    def issue_command(self, user_id: int, command: str, issue_id: str) -> str:
        issue = self.repository.get(issue_id) if issue_id else None
        if issue is None:
            return "Заявка не найдена. Используйте кнопки меню."
        if command == "/status":
            text, keyboard = self.issue_view(user_id, issue)
        elif command in {"/confirm", "/dispute"}:
            text, keyboard = self.vote(user_id, issue, approve=command == "/confirm")
        else:
            text, keyboard = self.staff_action(user_id, f"uk:{command[1:]}:{issue.id}")
        self._set_keyboard(user_id, keyboard or self.main_keyboard(user_id))
        return text
