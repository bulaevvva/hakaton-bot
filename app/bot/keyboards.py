"""Сборка inline-клавиатур MAX."""
from __future__ import annotations

from typing import Any

from ..domain import CATEGORIES, House, Issue, category_rule

Keyboard = list[dict[str, Any]]
Button = dict[str, Any]
Photo = dict[str, str]

MAX_PHOTOS_IN_MESSAGE = 5


def keyboard(rows: list[list[Button]]) -> Keyboard:
    return [{"type": "inline_keyboard", "payload": {"buttons": [row for row in rows if row]}}]


def button(text: str, payload: str) -> Button:
    return {"type": "callback", "text": text, "payload": payload}


def link(text: str, url: str) -> Button:
    return {"type": "link", "text": text, "url": url}


def clipboard(text: str, payload: str) -> Button:
    return {"type": "clipboard", "text": text, "payload": payload}


def geo(text: str = "📍 Отправить геолокацию") -> Button:
    return {"type": "request_geo_location", "text": text}


def rows_of(buttons: list[Button], width: int) -> list[list[Button]]:
    return [buttons[index:index + width] for index in range(0, len(buttons), width)]


def with_photos(photos: list[Photo], kb: Keyboard) -> Keyboard:
    images = []
    for photo in photos[:MAX_PHOTOS_IN_MESSAGE]:
        if photo.get("token"):
            images.append({"type": "image", "payload": {"token": photo["token"]}})
        elif photo.get("url"):
            images.append({"type": "image", "payload": {"url": photo["url"]}})
    return images + kb


def image(token: str) -> dict[str, Any]:
    return {"type": "image", "payload": {"token": token}}


def cancel_row() -> list[Button]:
    return [button("Отмена", "draft:cancel")]


def menu_row() -> list[Button]:
    return [button("🏠 Главное меню", "menu:back")]


def category_keyboard(house: House | None, entrance: str | None) -> Keyboard:
    buttons = [button(f"{rule.emoji} {rule.label}", f"rep:cat:{rule.key}") for rule in CATEGORIES]
    place = f"🏠 {house.address}, п. {entrance} · сменить" if house else "🏠 Выбрать дом"
    return keyboard(rows_of(buttons, 2) + [[button(place, "rep:house")], cancel_row()])


def house_choice_keyboard(houses: list[House], prefix: str, distances: dict[str, float] | None = None) -> Keyboard:
    rows = []
    for house in houses:
        suffix = f" · {int(distances[house.id])} м" if distances and house.id in distances else ""
        rows.append([button(f"🏠 {house.address}{suffix}", f"{prefix}{house.id}")])
    rows.append([geo()])
    rows.append(cancel_row())
    return keyboard(rows)


def entrance_keyboard(house: House, prefix: str) -> Keyboard:
    buttons = [button(str(number), f"{prefix}{number}") for number in range(1, house.entrances + 1)]
    return keyboard(rows_of(buttons, 5) + [cancel_row()])


def issue_list_keyboard(
    issues: list[Issue], page: int, pages: int, page_prefix: str, *, back: list[Button] | None = None
) -> Keyboard:
    rows = [
        [button(
            f"{category_rule(issue.category).emoji} {category_rule(issue.category).label[:16]} · "
            f"{issue.house[:18]} · 👥{len(issue.participants)}",
            f"iss:v:{issue.id}",
        )]
        for issue in issues
    ]
    pager = []
    if page > 0:
        pager.append(button("« Назад", f"{page_prefix}{page - 1}"))
    if page < pages - 1:
        pager.append(button("Далее »", f"{page_prefix}{page + 1}"))
    rows.append(pager)
    rows.append(back or menu_row())
    return keyboard(rows)
