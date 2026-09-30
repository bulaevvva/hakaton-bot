"""Сценарии сотрудников УК: диспетчер и исполнитель."""
from __future__ import annotations

from ..domain import (
    ACTIVE_STATUSES,
    FINISHED_STATUSES,
    STATUS_LABELS,
    Issue,
    IssueStatus,
    Role,
    category_rule,
    end_of_local_day,
    format_day,
    format_issue,
    is_overdue,
    priority_label,
    utcnow,
)
from . import keyboards as kb
from .core import BotCore, Draft, Outgoing, Reply
from .keyboards import Keyboard, Photo

# Фильтры очереди УК: ключ → (подпись, статусы, только просроченные).
UK_FILTERS: dict[str, tuple[str, frozenset[IssueStatus] | None, bool]] = {
    "active": ("Очередь", ACTIVE_STATUSES, False),
    "overdue": ("Просроченные", ACTIVE_STATUSES, True),
    "waiting": ("Ждут жителей", frozenset({IssueStatus.WAITING_CONFIRMATION}), False),
    "closed": ("Закрытые", FINISHED_STATUSES, False),
    "all": ("Все", None, False),
}
PLAN_OPTIONS = (("Сегодня", 0), ("Завтра", 1), ("За 3 дня", 3))


class StaffMixin(BotCore):
    def enter_demo_admin(self, user_id: int, code: str, name: str = "") -> str:
        """Демо-вход: /uk КОД делает администратором УК по умолчанию."""
        if not self.uk_access_code or code != self.uk_access_code:
            return "Код не принят."
        self.directory.add_staff(user_id, self.default_org.id, Role.ADMIN, name)
        self._set_keyboard(user_id, self.main_keyboard(user_id))
        return (
            f"✅ Демо-режим: вы администратор {self.default_org.name}.\n"
            "В меню появились «Очередь заявок», «Объявление» и «Управление УК»."
        )

    # ------------------------------------------------------------------ маршрутизация

    def staff_action(self, user_id: int, payload: str) -> Reply:
        staff = self.staff(user_id)
        if staff is None:
            return "Раздел доступен сотрудникам управляющей компании.", self.main_keyboard(user_id)
        parts = payload.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if payload == "mst:tasks":
            return self.master_tasks(user_id)
        if action == "done":
            return self.complete(user_id, parts[2])
        if staff.role not in {Role.ADMIN, Role.DISPATCHER}:
            return "Это действие доступно диспетчеру или администратору УК.", self.main_keyboard(user_id)
        if action == "list":
            filter_key = parts[2] if len(parts) > 2 else "active"
            page = self.to_int(parts[3]) if len(parts) > 3 else 0
            return self.queue_view(user_id, staff.org_id, filter_key, page), None
        if action == "announce":
            return self.announce_start(user_id, staff.org_id)
        if action == "ann":
            house = self.directory.get_house(parts[2])
            if house is None or house.org_id != staff.org_id:
                return "Дом не найден.", self.main_keyboard(user_id)
            self.draft(user_id, "uk_announce_text", house_id=house.id)
            return (
                f"📢 Объявление для дома {house.address}\n"
                "Напишите текст одним сообщением — его получат все жители дома в боте"
                + (" и чат дома." if house.chat_id else ".")
            ), kb.keyboard([kb.cancel_row()])
        if action == "photo_skip":
            self.drafts.pop(user_id, None)
            return "Готово. Жители получили просьбу подтвердить результат.", self.main_keyboard(user_id)

        issue = self.org_issue(user_id, parts[2] if len(parts) > 2 else "")
        if issue is None:
            return "Заявка не найдена или относится к другой УК.", self.main_keyboard(user_id)
        if action == "take":
            return self.take(user_id, issue)
        if action == "plan":
            return self.plan_view(issue, "Срок и сообщение для жителей")
        if action == "pl":
            return self.set_plan_days(user_id, issue, self.to_int(parts[3]))
        if action == "cmt":
            self.draft(user_id, "uk_comment", issue_id=issue.id)
            return (
                "💬 Напишите сообщение для жителей, например: «Мастер придёт завтра с 10 до 12» "
                "или «Ждём запчасть, срок — пятница»."
            ), kb.keyboard([kb.cancel_row()])
        if action == "asg":
            return self.assign_view(user_id, issue)
        if action == "asg2":
            return self.assign(user_id, issue, self.to_int(parts[3]))
        if action == "photo":
            self.draft(user_id, "uk_result_photo", issue_id=issue.id)
            return "📷 Пришлите фото результата одним или несколькими сообщениями.", self.photo_keyboard()
        return "Действие устарело.", self.main_keyboard(user_id)

    def org_issue(self, user_id: int, issue_id: str) -> Issue | None:
        issue = self.repository.get(issue_id)
        staff = self.staff(user_id)
        if issue is None or staff is None or issue.org_id != staff.org_id:
            return None
        return issue

    # ------------------------------------------------------------------ очередь

    def queue_view(self, user_id: int, org_id: str, filter_key: str, page: int) -> str:
        filter_key = filter_key if filter_key in UK_FILTERS else "active"
        label, statuses, overdue_only = UK_FILTERS[filter_key]
        now = utcnow()
        if filter_key in {"active", "overdue"}:
            issues = self.issue_service.queue(org_id, set(statuses), now)
        else:
            issues = self.issue_service.issues(org_id, set(statuses) if statuses else None)
        if overdue_only:
            issues = [issue for issue in issues if is_overdue(issue, now)]
        page_items, page, pages = self.paginate(issues, page)
        keyboard = kb.issue_list_keyboard(page_items, page, pages, f"uk:list:{filter_key}:")
        filters = [
            kb.button(("• " if key == filter_key else "") + title, f"uk:list:{key}:0")
            for key, (title, _, _) in UK_FILTERS.items()
        ]
        buttons = keyboard[0]["payload"]["buttons"]
        buttons[-1:-1] = kb.rows_of(filters, 3)
        self._set_keyboard(user_id, keyboard)

        active = self.issue_service.issues(org_id, set(ACTIVE_STATUSES))
        overdue = sum(1 for issue in active if is_overdue(issue, now))
        org = self.directory.get_org(org_id)
        header = (
            f"🏢 {org.name if org else 'УК'} · {label}\n"
            f"Открыто: {len(active)} · просрочено: {overdue}\n"
        )
        if not issues:
            return header + "\nВ этом разделе заявок нет."
        lines = []
        for item in page_items:
            rule = category_rule(item.category)
            mark = priority_label(item, now).split()[0] if item.status in ACTIVE_STATUSES else "·"
            late = " ⏰" if is_overdue(item, now) else ""
            lines.append(
                f"{mark} {rule.emoji} {rule.label} · {item.house}, п. {item.entrance}"
                f" · 👥 {len(item.participants)} · {STATUS_LABELS[item.status]}{late}"
            )
        note = f" (стр. {page + 1} из {pages})" if pages > 1 else ""
        return header + f"\nСначала самые срочные{note}:\n" + "\n".join(lines)

    # ------------------------------------------------------------------ работа с заявкой

    def take(self, user_id: int, issue: Issue) -> Reply:
        updated = self.issue_service.take_in_work(issue.id, user_id)
        if updated is None:
            return self.unavailable(user_id, issue)
        self.notify_participants(user_id, updated, "🛠 УК взяла заявку в работу", exclude=user_id)
        return self.plan_view(updated, "🛠 Заявка в работе. Укажите срок — жители его увидят")

    def plan_view(self, issue: Issue, title: str) -> Reply:
        rows = [[kb.button(label, f"uk:pl:{issue.id}:{days}") for label, days in PLAN_OPTIONS]]
        rows.append([kb.button("💬 Сообщение жителям", f"uk:cmt:{issue.id}")])
        rows.append([kb.button("👷 Назначить исполнителя", f"uk:asg:{issue.id}")])
        rows.append([kb.button("« К заявке", f"iss:v:{issue.id}")])
        return f"{title}\n\n" + format_issue(issue), kb.keyboard(rows)

    def set_plan_days(self, user_id: int, issue: Issue, days: int) -> Reply:
        updated = self.issue_service.set_plan(issue.id, user_id, planned_at=end_of_local_day(days))
        if updated is None:
            return self.unavailable(user_id, issue)
        self.notify_participants(
            user_id, updated, f"📅 УК назначила срок устранения: до {format_day(updated.planned_at)}",
            exclude=user_id,
        )
        return self.plan_view(updated, "📅 Срок сохранён, жители уведомлены")

    def assign_view(self, user_id: int, issue: Issue) -> Reply:
        masters = self.directory.list_staff(issue.org_id, Role.MASTER)
        if not masters:
            hint = (
                "Пригласите исполнителя: «🛠 Управление УК» → «Сотрудники»."
                if self.is_admin(user_id) else "Попросите администратора УК пригласить исполнителя."
            )
            return f"В УК пока нет исполнителей. {hint}", kb.keyboard([[kb.button("« К заявке", f"iss:v:{issue.id}")]])
        rows = [
            [kb.button(f"👷 {member.name or 'Исполнитель ' + str(member.user_id)}", f"uk:asg2:{issue.id}:{member.user_id}")]
            for member in masters
        ]
        rows.append([kb.button("« К заявке", f"iss:v:{issue.id}")])
        return "Кому передать заявку?", kb.keyboard(rows)

    def assign(self, user_id: int, issue: Issue, master_id: int) -> Reply:
        master = self.staff(master_id)
        if master is None or master.org_id != issue.org_id or master.role is not Role.MASTER:
            return "Исполнитель не найден.", kb.keyboard([[kb.button("« К заявке", f"iss:v:{issue.id}")]])
        updated = self.issue_service.assign(issue.id, master_id, user_id)
        if updated is None:
            return self.unavailable(user_id, issue)
        self.to_user(
            user_id, master_id,
            "👷 Вам назначена заявка. Когда закончите, нажмите «Выполнено» и приложите фото.\n\n"
            + format_issue(updated),
            kb.with_photos(updated.photos, self.issue_keyboard(master_id, updated)),
        )
        return self.plan_view(updated, f"👷 Заявка передана: {master.name or master_id}")

    def complete(self, user_id: int, issue_id: str) -> Reply:
        issue = self.repository.get(issue_id)
        staff = self.staff(user_id)
        allowed = issue is not None and staff is not None and issue.org_id == staff.org_id and (
            staff.role in {Role.ADMIN, Role.DISPATCHER} or issue.assignee_id == user_id
        )
        if not allowed:
            return "Отметить выполнение может диспетчер или назначенный исполнитель.", self.main_keyboard(user_id)
        updated = self.issue_service.mark_completed_by_uk(issue.id, user_id)
        if updated is None:
            return self.unavailable(user_id, issue)
        self.notify_participants(
            user_id, updated,
            "🔔 УК сообщает, что проблема устранена. Проверьте и ответьте — итог решает большинство соседей",
            exclude=user_id,
        )
        if staff.role is Role.MASTER:
            self.notify_staff(user_id, updated, "✅ Исполнитель отметил выполнение")
        self.draft(user_id, "uk_result_photo", issue_id=updated.id)
        return (
            "✅ Выполнение отмечено, жители получили просьбу подтвердить.\n\n"
            "📷 Пришлите фото результата — оно придёт жителям и защитит УК от "
            "необоснованных претензий. Или нажмите «Без фото»."
        ), self.photo_keyboard()

    @staticmethod
    def photo_keyboard() -> Keyboard:
        return kb.keyboard([[kb.button("Без фото", "uk:photo_skip")]])

    def unavailable(self, user_id: int, issue: Issue) -> Reply:
        current = self.repository.get(issue.id) or issue
        return (
            f"Действие недоступно: заявка в статусе «{STATUS_LABELS[current.status]}».\n\n"
            + format_issue(current)
        ), self.issue_keyboard(user_id, current)

    def master_tasks(self, user_id: int) -> Reply:
        tasks = self.issue_service.assigned_to(user_id)
        page_items, page, pages = self.paginate(tasks, 0)
        if not tasks:
            return "🧰 Назначенных задач нет.", self.main_keyboard(user_id)
        return (
            "🧰 Мои задачи\n\n" + "\n".join(
                f"{category_rule(item.category).emoji} {category_rule(item.category).label} · "
                f"{item.house}, п. {item.entrance}"
                + (f" · до {format_day(item.planned_at)}" if item.planned_at else "")
                for item in page_items
            )
        ), kb.issue_list_keyboard(page_items, page, pages, "mst:tasks:")

    # ------------------------------------------------------------------ объявления

    def announce_start(self, user_id: int, org_id: str) -> Reply:
        houses = self.directory.list_houses(org_id)
        if not houses:
            return "В справочнике УК пока нет домов.", self.main_keyboard(user_id)
        rows = [[kb.button(f"🏠 {house.address}", f"uk:ann:{house.id}")] for house in houses[:20]]
        rows.append(kb.cancel_row())
        return "📢 Объявление жителям. Для какого дома?", kb.keyboard(rows)

    # ------------------------------------------------------------------ шаги с вводом текста

    def staff_step(self, user_id: int, draft: Draft, text: str, photos: list[Photo]) -> str:
        staff = self.staff(user_id)
        if staff is None:
            self.drafts.pop(user_id, None)
            return "Раздел доступен сотрудникам УК."
        if draft.step == "uk_result_photo":
            if not photos:
                self._set_keyboard(user_id, self.photo_keyboard())
                return "Пришлите фото результата или нажмите «Без фото»."
            issue = self.issue_service.add_result_photos(draft.issue_id or "", photos)
            self.drafts.pop(user_id, None)
            self._set_keyboard(user_id, self.main_keyboard(user_id))
            if issue is None:
                return "Заявка уже не ждёт подтверждения — фото не добавлено."
            self.notify_participants(user_id, issue, "📷 УК приложила фото результата", exclude=user_id)
            return f"Фото результата добавлено ({len(issue.result_photos)}) и отправлено жителям."
        if draft.step == "uk_comment":
            if len(text) < 3:
                return "Напишите сообщение для жителей текстом."
            issue = self.issue_service.set_plan(draft.issue_id or "", user_id, comment=text)
            self.drafts.pop(user_id, None)
            if issue is None:
                self._set_keyboard(user_id, self.main_keyboard(user_id))
                return "Заявка уже закрыта."
            self.notify_participants(user_id, issue, f"💬 Сообщение от УК: {issue.uk_comment}", exclude=user_id)
            text, keyboard = self.plan_view(issue, "💬 Сообщение отправлено жителям")
            self._set_keyboard(user_id, keyboard)
            return text
        if draft.step == "uk_announce_text":
            house = self.directory.get_house(draft.house_id or "")
            if len(text) < 5 or house is None:
                return "Текст объявления слишком короткий."
            self.drafts.pop(user_id, None)
            announcement, recipients = self.issue_service.announce(house, text, user_id)
            message = f"📢 Объявление УК для дома {house.address}\n\n{announcement.text}"
            for recipient in recipients:
                if recipient != user_id:
                    self.to_user(user_id, recipient, message, None)
            if house.chat_id is not None:
                self._queue(user_id, Outgoing("chat", house.chat_id, message))
            self._set_keyboard(user_id, self.main_keyboard(user_id))
            chat_note = " и в чат дома" if house.chat_id else ""
            return f"Объявление отправлено жителям дома {house.address}: {len(recipients)}{chat_note}."
        self.drafts.pop(user_id, None)
        return "Сценарий устарел. Откройте меню заново."

