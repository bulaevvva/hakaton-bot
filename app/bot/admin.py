"""Администратор УК: справочник домов, QR-плакаты, сотрудники, показатели.
Администратор сервиса: подключение новых УК."""
from __future__ import annotations

from typing import Any

from ..domain import CHAIRMAN_INVITE, ROLE_LABELS, House, Role
from . import keyboards as kb
from .core import BotCore, Draft, Reply


def _hours(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 48:
        return f"{value:.0f} ч"
    return f"{value / 24:.1f} дн".replace(".", ",")


def _percent(value: int | None) -> str:
    return "—" if value is None else f"{value}%"


def metrics_text(title: str, stats: dict[str, Any]) -> str:
    """Пять метрик пилота из описания продукта плюс оперативная сводка."""
    return (
        f"📊 {title}\n\n"
        f"Обращений жителей: {stats['total_reports']} → заявок: {stats['total_issues']}\n"
        f"1️⃣ Доля дублей: {_percent(stats['duplicate_share'])} — соседи присоединились "
        f"вместо новой заявки (по оповещениям: {stats['joined_from_alerts']})\n"
        f"2️⃣ Повторных обращений после закрытия: {stats['repeat_issues']}\n"
        f"3️⃣ Подтверждено жителями: {_percent(stats['confirmed_share'])}\n"
        f"4️⃣ Оспорено жителями: {_percent(stats['disputed_share'])} · "
        f"без ответа: {_percent(stats['no_response_share'])}\n"
        f"5️⃣ От подачи до подтверждённого устранения: {_hours(stats['avg_to_confirmed_hours'])}\n\n"
        f"Реакция УК: {_hours(stats['avg_reaction_hours'])} · выполнение: {_hours(stats['avg_resolution_hours'])}\n"
        f"Сейчас: открыто {stats['active']} · просрочено {stats['overdue']} · "
        f"ждут подтверждения {stats['waiting_confirmation']}"
    )


class AdminMixin(BotCore):
    # ------------------------------------------------------------------ сервис

    def platform_command(self, user_id: int, command: str, argument: str) -> str:
        if not self.is_platform_admin(user_id):
            return "Команда доступна администратору сервиса."
        if command == "/orgs":
            orgs = self.repository.select(type(self.default_org))
            return "Подключённые УК:\n" + "\n".join(
                f"• {org.name} — домов: {len(self.directory.list_houses(org.id))}, "
                f"сотрудников: {len(self.directory.list_staff(org.id))}"
                for org in orgs
            )
        if len(argument) < 3:
            return "Укажите название: /org_new УК «Название»"
        org = self.directory.create_org(argument)
        invite = self.directory.create_invite(org.id, Role.ADMIN.value, user_id)
        link = self.invite_link(invite.code)
        self._set_keyboard(user_id, kb.keyboard([[kb.clipboard("📋 Скопировать ссылку", link)]]))
        return (
            f"✅ Подключена {org.name}.\nОтправьте ссылку её администратору — по ней он "
            f"войдёт в роль и заполнит справочник домов:\n{link}"
        )

    # ------------------------------------------------------------------ маршрутизация

    def admin_action(self, user_id: int, payload: str) -> Reply:
        staff = self.staff(user_id)
        if staff is None or staff.role is not Role.ADMIN:
            return "Раздел доступен администратору УК.", self.main_keyboard(user_id)
        parts = payload.split(":")
        action = parts[1] if len(parts) > 1 else "menu"
        arg = parts[2] if len(parts) > 2 else ""
        org_id = staff.org_id
        if action == "menu":
            return self.admin_menu(org_id)
        if action == "houses":
            return self.houses_view(org_id)
        if action == "add":
            self.draft(user_id, "adm_addr")
            return (
                "➕ Новый дом. Напишите адрес так, как его должны видеть жители, "
                "например: ул. Ленина, 10"
            ), kb.keyboard([kb.cancel_row()])
        if action == "staff":
            return self.staff_view(user_id, org_id)
        if action == "inv":
            return self.invite_staff(user_id, org_id, arg)
        if action == "m":
            return self.member_view(user_id, org_id, self.to_int(arg))
        if action in {"rm", "rm2"}:
            return self.remove_member(user_id, org_id, self.to_int(arg), confirmed=action == "rm2")
        if action == "stats":
            org = self.directory.get_org(org_id)
            return metrics_text(f"Показатели · {org.name if org else 'УК'}", self.issue_service.stats(org_id)), \
                kb.keyboard([[kb.button("« Управление", "adm:menu")]])
        if action == "geo_skip":
            return self.finish_new_house(user_id, None), None

        house = self.directory.get_house(arg)
        if house is None or house.org_id != org_id:
            return "Дом не найден.", kb.keyboard([[kb.button("« Дома", "adm:houses")]])
        if action == "h":
            return self.house_card(house)
        if action == "qr":
            self.queue_posters(user_id, house)
            return (
                f"🖨 Готовлю QR-плакаты для {house.address}: {house.entrances} шт., по одному на подъезд. "
                "Распечатайте и повесьте у входа — жители откроют бота с уже выбранным адресом."
            ), self.house_keyboard(house)
        if action == "chair":
            invite = self.directory.create_invite(org_id, CHAIRMAN_INVITE, user_id, house.id)
            link = self.invite_link(invite.code)
            return (
                f"👤 Ссылка для председателя совета дома {house.address}:\n{link}\n\n"
                "Председатель подписывает акты приёмки работ УК — в боте его ответ "
                "«устранена / осталась» решает итог по заявке."
            ), kb.keyboard([[kb.clipboard("📋 Скопировать ссылку", link)], [kb.button("« К дому", f"adm:h:{house.id}")]])
        if action == "chat":
            return self.chat_view(house)
        if action == "unchat":
            self.directory.unlink_chat(house)
            return self.house_card(self.directory.get_house(house.id))
        if action == "geo":
            self.draft(user_id, "adm_geo", house_id=house.id)
            return (
                f"📍 Отправьте геолокацию, стоя у дома {house.address}. По ней жители "
                "смогут находить дом без ввода адреса."
            ), kb.keyboard([[kb.geo()], kb.cancel_row()])
        if action == "ent":
            self.draft(user_id, "adm_ent", house_id=house.id)
            return f"Сколько подъездов в доме {house.address}? Напишите число.", kb.keyboard([kb.cancel_row()])
        if action == "hs":
            return metrics_text(f"Показатели · {house.address}", self.issue_service.stats(org_id, house.id)), \
                kb.keyboard([[kb.button("« К дому", f"adm:h:{house.id}")]])
        return "Действие устарело.", self.admin_menu(org_id)[1]

    # ------------------------------------------------------------------ экраны

    def admin_menu(self, org_id: str) -> Reply:
        org = self.directory.get_org(org_id)
        houses = self.directory.list_houses(org_id)
        staff = self.directory.list_staff(org_id)
        stats = self.issue_service.stats(org_id)
        return (
            f"🛠 Управление · {org.name if org else 'УК'}\n\n"
            f"Домов: {len(houses)} · сотрудников: {len(staff)}\n"
            f"Открыто заявок: {stats['active']} · просрочено: {stats['overdue']}"
        ), kb.keyboard([
            [kb.button("🏠 Дома и QR-коды", "adm:houses"), kb.button("👥 Сотрудники", "adm:staff")],
            [kb.button("📊 Показатели", "adm:stats")],
            kb.menu_row(),
        ])

    def houses_view(self, org_id: str) -> Reply:
        houses = self.directory.list_houses(org_id)
        rows = []
        for house in houses[:30]:
            active = len(self.issue_service.house_issues(house.id))
            rows.append([kb.button(f"🏠 {house.address} · открыто {active}", f"adm:h:{house.id}")])
        rows.append([kb.button("➕ Добавить дом", "adm:add")])
        rows.append([kb.button("« Управление", "adm:menu")])
        text = "🏠 Справочник домов" if houses else "🏠 В справочнике пока нет домов. Добавьте первый."
        return text, kb.keyboard(rows)

    def house_card(self, house: House) -> Reply:
        active = len(self.issue_service.house_issues(house.id))
        residents = len(self.directory.residents_in_scope(house.id))
        return (
            f"🏠 {house.address}\n\n"
            f"Подъездов: {house.entrances}\n"
            f"Жителей в боте: {residents}\n"
            f"Открытых заявок: {active}\n"
            f"Геолокация: {'✅ есть' if house.latitude is not None else '— нет'}\n"
            f"Председатель совета: {'✅ назначен' if house.chairman_id else '— нет'}\n"
            f"Чат дома: {'✅ подключён' if house.chat_id else '— не подключён'}"
        ), self.house_keyboard(house)

    @staticmethod
    def house_keyboard(house: House) -> list[dict[str, Any]]:
        return kb.keyboard([
            [kb.button("🖨 QR-плакаты", f"adm:qr:{house.id}"), kb.button("📊 Показатели", f"adm:hs:{house.id}")],
            [kb.button("👤 Председатель", f"adm:chair:{house.id}"), kb.button("💬 Чат дома", f"adm:chat:{house.id}")],
            [kb.button("📍 Геолокация", f"adm:geo:{house.id}"), kb.button("🔢 Подъезды", f"adm:ent:{house.id}")],
            [kb.button("« Дома", "adm:houses")],
        ])

    def chat_view(self, house: House) -> Reply:
        if house.chat_id is not None:
            return (
                f"💬 Чат дома {house.address} подключён. Туда уходят оповещения о проблемах "
                "подъезда и всего дома, итоги подтверждения и объявления УК."
            ), kb.keyboard([[kb.button("Отключить чат", f"adm:unchat:{house.id}")], [kb.button("« К дому", f"adm:h:{house.id}")]])
        code = self.directory.start_chat_link(house)
        return (
            f"💬 Подключение чата дома {house.address} (по желанию УК)\n\n"
            "1. Администратор чата добавляет бота в чат дома.\n"
            f"2. Отправляет в чат команду: /link {code}\n\n"
            "После этого массовые проблемы будут дублироваться в чат с кнопкой "
            "«У меня тоже». Без чата всё работает через личные сообщения."
        ), kb.keyboard([[kb.clipboard("📋 Скопировать команду", f"/link {code}")], [kb.button("« К дому", f"adm:h:{house.id}")]])

    def staff_view(self, user_id: int, org_id: str) -> Reply:
        members = self.directory.list_staff(org_id)
        rows = [
            [kb.button(
                f"{'🛠' if member.role is Role.ADMIN else '👷' if member.role is Role.MASTER else '🎧'} "
                f"{member.name or member.user_id} — {ROLE_LABELS[member.role]}",
                f"adm:m:{member.user_id}",
            )]
            for member in members
        ]
        rows.append([kb.button("➕ Диспетчер", "adm:inv:dispatcher"), kb.button("➕ Исполнитель", "adm:inv:master")])
        rows.append([kb.button("➕ Администратор", "adm:inv:admin")])
        rows.append([kb.button("« Управление", "adm:menu")])
        return "👥 Сотрудники УК\n\nНовые сотрудники подключаются по одноразовой ссылке.", kb.keyboard(rows)

    def invite_staff(self, user_id: int, org_id: str, role: str) -> Reply:
        if role not in {item.value for item in Role}:
            return "Неизвестная роль.", self.staff_view(user_id, org_id)[1]
        invite = self.directory.create_invite(org_id, role, user_id)
        link = self.invite_link(invite.code)
        return (
            f"Ссылка-приглашение: {ROLE_LABELS[Role(role)]}\n{link}\n\n"
            "Ссылка одноразовая: отправьте её сотруднику, он откроет бота и получит доступ."
        ), kb.keyboard([[kb.clipboard("📋 Скопировать ссылку", link)], [kb.button("« Сотрудники", "adm:staff")]])

    def member_view(self, user_id: int, org_id: str, member_id: int) -> Reply:
        member = self.staff(member_id)
        if member is None or member.org_id != org_id:
            return "Сотрудник не найден.", self.staff_view(user_id, org_id)[1]
        rows = [[kb.button("« Сотрудники", "adm:staff")]]
        if member_id != user_id:
            rows.insert(0, [kb.button("Отключить доступ", f"adm:rm:{member_id}")])
        return (
            f"👤 {member.name or member.user_id}\nРоль: {ROLE_LABELS[member.role]}"
        ), kb.keyboard(rows)

    def remove_member(self, user_id: int, org_id: str, member_id: int, *, confirmed: bool) -> Reply:
        member = self.staff(member_id)
        if member is None or member.org_id != org_id or member_id == user_id:
            return "Этого сотрудника нельзя отключить.", self.staff_view(user_id, org_id)[1]
        if not confirmed:
            return (
                f"Отключить доступ сотрудника {member.name or member_id}?"
            ), kb.keyboard([[kb.button("Да, отключить", f"adm:rm2:{member_id}")], [kb.button("Отмена", "adm:staff")]])
        self.directory.remove_staff(member_id)
        text, keyboard = self.staff_view(user_id, org_id)
        return "Доступ отключён.\n\n" + text, keyboard

    # ------------------------------------------------------------------ ввод текста

    def admin_step(
        self, user_id: int, draft: Draft, text: str, location: tuple[float, float] | None
    ) -> str:
        staff = self.staff(user_id)
        if staff is None or staff.role is not Role.ADMIN:
            self.drafts.pop(user_id, None)
            return "Раздел доступен администратору УК."
        if draft.step == "adm_addr":
            if len(text) < 5:
                return "Напишите адрес полностью, например: ул. Ленина, 10"
            existing = self.directory.find_house_by_key(staff.org_id, text)
            if existing is not None:
                self.drafts.pop(user_id, None)
                card, keyboard = self.house_card(existing)
                self._set_keyboard(user_id, keyboard)
                return "Такой дом уже есть в справочнике.\n\n" + card
            draft.value = text[:200]
            draft.step = "adm_ent"
            self._set_keyboard(user_id, kb.keyboard([kb.cancel_row()]))
            return f"🏠 {draft.value}\nСколько в доме подъездов? Напишите число."
        if draft.step == "adm_ent":
            count = self.to_int(text)
            if not 1 <= count <= 40:
                return "Напишите число подъездов от 1 до 40."
            if draft.house_id:
                house = self.directory.get_house(draft.house_id)
                house.entrances = count
                self.directory.update_house(house)
                self.drafts.pop(user_id, None)
                card, keyboard = self.house_card(house)
                self._set_keyboard(user_id, keyboard)
                return "Число подъездов обновлено.\n\n" + card
            draft.entrance = str(count)
            draft.step = "adm_geo"
            self._set_keyboard(user_id, kb.keyboard([[kb.geo()], [kb.button("Пропустить", "adm:geo_skip")], kb.cancel_row()]))
            return (
                "📍 Отправьте геолокацию, стоя у дома, — жители смогут находить его без "
                "ввода адреса. Или нажмите «Пропустить»."
            )
        if draft.step == "adm_geo":
            if location is None:
                return "Отправьте геолокацию кнопкой или нажмите «Пропустить»."
            if draft.house_id:
                house = self.directory.get_house(draft.house_id)
                house.latitude, house.longitude = location
                self.directory.update_house(house)
                self.drafts.pop(user_id, None)
                card, keyboard = self.house_card(house)
                self._set_keyboard(user_id, keyboard)
                return "Геолокация сохранена.\n\n" + card
            return self.finish_new_house(user_id, location)
        self.drafts.pop(user_id, None)
        return "Сценарий устарел. Откройте «Управление УК» заново."

    def finish_new_house(self, user_id: int, location: tuple[float, float] | None) -> str:
        draft = self.drafts.pop(user_id, None)
        staff = self.staff(user_id)
        if draft is None or not draft.value or staff is None:
            return "Сценарий устарел. Откройте «Управление УК» заново."
        house = self.directory.add_house(
            staff.org_id, draft.value, self.to_int(draft.entrance or "1"),
            *(location or (None, None)),
        )
        card, keyboard = self.house_card(house)
        self._set_keyboard(user_id, keyboard)
        return "✅ Дом добавлен. Теперь напечатайте QR-плакаты для подъездов.\n\n" + card
