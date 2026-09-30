"""Бот «Дом: проблема решена»: житель, председатель совета дома, сотрудники УК."""
from __future__ import annotations

from .admin import AdminMixin, metrics_text
from .core import BOT_LINK_PLACEHOLDER, PAGE_SIZE, BotCore, Draft
from .resident import ResidentMixin
from .staff import UK_FILTERS, StaffMixin


class BotApplication(ResidentMixin, StaffMixin, AdminMixin, BotCore):
    """Собирает сценарии всех ролей поверх общего ядра."""


__all__ = [
    "BOT_LINK_PLACEHOLDER",
    "PAGE_SIZE",
    "UK_FILTERS",
    "BotApplication",
    "Draft",
    "metrics_text",
]
