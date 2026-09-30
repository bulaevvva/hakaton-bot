from __future__ import annotations

import dataclasses
import json
import sqlite3
import types
import typing
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

from .domain import (
    Announcement,
    House,
    Issue,
    IssueEvent,
    IssueStatus,
    Organization,
    Resident,
    StaffInvite,
    StaffMember,
    normalize_address,
)

T = TypeVar("T")
MATCH_WINDOW = timedelta(hours=72)

# Таблица и первичный ключ для каждой сущности.
TABLES: dict[type, tuple[str, str]] = {
    Organization: ("organizations", "id"),
    StaffMember: ("staff", "user_id"),
    House: ("houses", "id"),
    Resident: ("residents", "user_id"),
    StaffInvite: ("staff_invites", "code"),
    IssueEvent: ("issue_events", "id"),
    Announcement: ("house_announcements", "id"),
    Issue: ("issues", "id"),
}
# Поля, по которым часто ищем, — индексируются.
INDEXES = {
    House: ["org_id"],
    StaffMember: ["org_id"],
    Resident: ["house_id"],
    IssueEvent: ["issue_id"],
    Announcement: ["house_id"],
}


# ---------------------------------------------------------------- dataclass ↔ строка


@cache
def _columns(cls: type) -> tuple[tuple[str, str, type], ...]:
    """(имя, вид, тип) для каждого поля dataclass: int, float, bool, datetime, str, enum, set, json."""
    hints = typing.get_type_hints(cls)
    result = []
    for item in dataclasses.fields(cls):
        tp = hints[item.name]
        if typing.get_origin(tp) in (typing.Union, types.UnionType):
            tp = next(arg for arg in typing.get_args(tp) if arg is not type(None))
        origin = typing.get_origin(tp) or tp
        if origin is bool:
            kind = "bool"
        elif origin is int:
            kind = "int"
        elif origin is float:
            kind = "float"
        elif origin is datetime:
            kind = "datetime"
        elif isinstance(origin, type) and issubclass(origin, Enum):
            kind = "enum"
        elif origin is str:
            kind = "str"
        elif origin is set:
            kind = "set"
        else:
            kind = "json"
        result.append((item.name, kind, tp))
    return tuple(result)


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _from_row(cls: type[T], row: dict[str, Any]) -> T:
    values = {}
    for name, kind, tp in _columns(cls):
        if name not in row:
            continue
        value = row[name]
        if value is None:
            values[name] = None
            continue
        if kind in {"json", "set"} and isinstance(value, str):
            value = json.loads(value)
        if kind == "datetime":
            value = _as_datetime(value)
        elif kind == "bool":
            value = bool(value)
        elif kind == "enum":
            value = tp(value)
        elif kind == "set":
            value = set(value)
        elif kind == "int":
            value = int(value)
        elif kind == "float":
            value = float(value)
        values[name] = value
    return cls(**values)


def _clone(obj: T) -> T:
    """Копия для in-memory хранилища: внешние изменения не должны менять «базу» без put()."""
    return dataclasses.replace(obj, **{
        name: type(getattr(obj, name))(getattr(obj, name))
        for name, kind, _ in _columns(type(obj))
        if kind in {"set", "json"} and isinstance(getattr(obj, name), (set, list, dict))
    })


# ---------------------------------------------------------------- память


class InMemoryIssueRepository:
    """Хранилище для тестов.

    Заявки хранятся как есть (тесты меняют их напрямую), справочники — копиями.
    """

    def __init__(self) -> None:
        self._issues: dict[str, Issue] = {}
        self._objects: dict[type, dict[Any, Any]] = {cls: {} for cls in TABLES if cls is not Issue}
        self._drafts: dict[int, dict[str, Any]] = {}

    # справочники

    def get_obj(self, cls: type[T], key: Any) -> T | None:
        obj = self._objects[cls].get(key)
        return _clone(obj) if obj is not None else None

    def put(self, obj: T) -> T:
        _, key = TABLES[type(obj)]
        self._objects[type(obj)][getattr(obj, key)] = _clone(obj)
        return obj

    def delete_obj(self, cls: type, key: Any) -> None:
        self._objects[cls].pop(key, None)

    def select(self, cls: type[T], **equals: Any) -> list[T]:
        return [
            _clone(obj) for obj in self._objects[cls].values()
            if all(getattr(obj, name) == value for name, value in equals.items())
        ]

    # заявки

    def find_similar(
        self, house_id: str, entrance: str | None, category: str, now: datetime
    ) -> Issue | None:
        threshold = now - MATCH_WINDOW
        candidates = [
            issue for issue in self._issues.values()
            if issue.status in {IssueStatus.OPEN, IssueStatus.IN_PROGRESS}
            and issue.created_at >= threshold
            and issue.house_id == house_id
            and (entrance is None or issue.entrance == entrance)
            and issue.category == category
        ]
        return max(candidates, key=lambda item: item.created_at, default=None)

    def add(self, issue: Issue) -> Issue:
        self._issues[issue.id] = issue
        return issue

    def get(self, issue_id: str) -> Issue | None:
        return self._issues.get(issue_id)

    def save(self, issue: Issue) -> Issue:
        self._issues[issue.id] = issue
        return issue

    def list_for_user(self, user_id: int) -> list[Issue]:
        return sorted(
            (issue for issue in self._issues.values() if user_id in issue.participants),
            key=lambda issue: issue.updated_at,
            reverse=True,
        )

    def list_for_uk(self, statuses: set[IssueStatus] | None = None) -> list[Issue]:
        issues = self._issues.values()
        if statuses:
            issues = (issue for issue in issues if issue.status in statuses)
        return sorted(issues, key=lambda issue: issue.updated_at, reverse=True)

    def list_waiting_confirmation(self) -> list[Issue]:
        return self.list_for_uk({IssueStatus.WAITING_CONFIRMATION})

    # черновики

    def get_draft(self, user_id: int) -> dict[str, Any] | None:
        draft = self._drafts.get(user_id)
        return json.loads(json.dumps(draft)) if draft is not None else None

    def save_draft(self, user_id: int, data: dict[str, Any]) -> None:
        self._drafts[user_id] = json.loads(json.dumps(data))

    def delete_draft(self, user_id: int) -> None:
        self._drafts.pop(user_id, None)


# ---------------------------------------------------------------- SQL


class _SqlIssueRepository:
    """Общий SQL для SQLite и PostgreSQL. Схема строится из dataclass-сущностей,
    недостающие колонки добавляются при старте — старые базы мигрируют сами."""

    ph = "?"
    postgres = False
    connection: Any

    # диалект

    def _sql_type(self, kind: str) -> str:
        if self.postgres:
            return {
                "int": "BIGINT", "float": "DOUBLE PRECISION", "bool": "BOOLEAN",
                "datetime": "TIMESTAMPTZ", "set": "JSONB", "json": "JSONB",
            }.get(kind, "TEXT")
        return {"int": "INTEGER", "float": "REAL", "bool": "INTEGER"}.get(kind, "TEXT")

    def _to_db(self, kind: str, value: Any) -> Any:
        if value is None:
            return None
        if kind == "set":
            return json.dumps(sorted(value))
        if kind == "json":
            return json.dumps(value, ensure_ascii=False)
        if kind == "enum":
            return value.value
        if kind == "datetime" and not self.postgres:
            return value.isoformat()
        if kind == "bool" and not self.postgres:
            return int(value)
        return value

    def _placeholder(self, kind: str) -> str:
        return f"{self.ph}::jsonb" if self.postgres and kind in {"set", "json"} else self.ph

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        return self.connection.execute(sql.replace("?", self.ph), params)

    def _existing_columns(self, table: str) -> set[str]:
        if self.postgres:
            rows = self._execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,)
            ).fetchall()
            return {dict(row)["column_name"] for row in rows}
        return {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}

    def _create_schema(self) -> None:
        for cls, (table, key) in TABLES.items():
            columns = _columns(cls)
            existing = self._existing_columns(table)
            if not existing:
                definitions = ", ".join(
                    f"{name} {self._sql_type(kind)}" + (" PRIMARY KEY" if name == key else "")
                    for name, kind, _ in columns
                )
                self._execute(f"CREATE TABLE {table} ({definitions})")
            else:
                for name, kind, _ in columns:
                    if name not in existing:
                        self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {self._sql_type(kind)}")
            for column in INDEXES.get(cls, []):
                self._execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{column} ON {table} ({column})")
        self._execute(
            "CREATE INDEX IF NOT EXISTS idx_issues_house ON issues (house_id, category, status)"
        )
        draft_data = "JSONB" if self.postgres else "TEXT"
        self._execute(
            f"CREATE TABLE IF NOT EXISTS drafts (user_id {self._sql_type('int')} PRIMARY KEY, "
            f"data {draft_data} NOT NULL, updated_at {self._sql_type('datetime')} NOT NULL)"
        )
        self._migrate_legacy_issues()
        self.connection.commit()

    def _migrate_legacy_issues(self) -> None:
        """Старые заявки: дописываем ключ адреса и пустые списки новых полей."""
        rows = self._execute(
            "SELECT id, house, house_key FROM issues WHERE house_key IS NULL OR house_key = ''"
        ).fetchall()
        for row in map(dict, rows):
            self._execute(
                "UPDATE issues SET house_key = ? WHERE id = ?",
                (normalize_address(row["house"]), row["id"]),
            )
        for name, kind, _ in _columns(Issue):
            if kind in {"set", "json"}:
                self._execute(
                    f"UPDATE issues SET {name} = {self._placeholder(kind)} WHERE {name} IS NULL",
                    ("[]",),
                )
            elif kind in {"int", "bool"}:
                self._execute(f"UPDATE issues SET {name} = ? WHERE {name} IS NULL", (self._to_db(kind, 0 if kind == "int" else False),))
            elif kind == "str" and name in {"house_id", "org_id", "uk_comment"}:
                self._execute(f"UPDATE issues SET {name} = '' WHERE {name} IS NULL")

    # общие операции

    def put(self, obj: T) -> T:
        table, key = TABLES[type(obj)]
        columns = _columns(type(obj))
        names = ", ".join(name for name, _, _ in columns)
        placeholders = ", ".join(self._placeholder(kind) for _, kind, _ in columns)
        updates = ", ".join(f"{name} = excluded.{name}" for name, _, _ in columns if name != key)
        self._execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders}) "
            f"ON CONFLICT ({key}) DO UPDATE SET {updates}",
            tuple(self._to_db(kind, getattr(obj, name)) for name, kind, _ in columns),
        )
        self.connection.commit()
        return obj

    def get_obj(self, cls: type[T], key: Any) -> T | None:
        table, key_column = TABLES[cls]
        row = self._execute(f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)).fetchone()
        return _from_row(cls, dict(row)) if row else None

    def delete_obj(self, cls: type, key: Any) -> None:
        table, key_column = TABLES[cls]
        self._execute(f"DELETE FROM {table} WHERE {key_column} = ?", (key,))
        self.connection.commit()

    def select(self, cls: type[T], **equals: Any) -> list[T]:
        table, _ = TABLES[cls]
        kinds = {name: kind for name, kind, _ in _columns(cls)}
        where = " AND ".join(f"{name} = ?" for name in equals) or "1 = 1"
        rows = self._execute(
            f"SELECT * FROM {table} WHERE {where}",
            tuple(self._to_db(kinds[name], value) for name, value in equals.items()),
        ).fetchall()
        return [_from_row(cls, dict(row)) for row in rows]

    # заявки

    def find_similar(
        self, house_id: str, entrance: str | None, category: str, now: datetime
    ) -> Issue | None:
        sql = (
            "SELECT * FROM issues WHERE house_id = ? AND category = ? AND status IN (?, ?) "
            "AND created_at >= ?"
        )
        params: list[Any] = [
            house_id, category, IssueStatus.OPEN.value, IssueStatus.IN_PROGRESS.value,
            self._to_db("datetime", now - MATCH_WINDOW),
        ]
        if entrance is not None:
            sql += " AND entrance = ?"
            params.append(entrance)
        row = self._execute(sql + " ORDER BY created_at DESC LIMIT 1", tuple(params)).fetchone()
        return _from_row(Issue, dict(row)) if row else None

    def add(self, issue: Issue) -> Issue:
        return self.put(issue)

    def save(self, issue: Issue) -> Issue:
        return self.put(issue)

    def get(self, issue_id: str) -> Issue | None:
        return self.get_obj(Issue, issue_id)

    def list_for_user(self, user_id: int) -> list[Issue]:
        return [issue for issue in self.list_for_uk() if user_id in issue.participants]

    def list_for_uk(self, statuses: set[IssueStatus] | None = None) -> list[Issue]:
        rows = self._execute("SELECT * FROM issues ORDER BY updated_at DESC").fetchall()
        issues = [_from_row(Issue, dict(row)) for row in rows]
        return [issue for issue in issues if not statuses or issue.status in statuses]

    def list_waiting_confirmation(self) -> list[Issue]:
        return self.list_for_uk({IssueStatus.WAITING_CONFIRMATION})

    # черновики

    def get_draft(self, user_id: int) -> dict[str, Any] | None:
        row = self._execute("SELECT data FROM drafts WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        data = dict(row)["data"]
        return json.loads(data) if isinstance(data, str) else dict(data)

    def save_draft(self, user_id: int, data: dict[str, Any]) -> None:
        self._execute(
            f"INSERT INTO drafts (user_id, data, updated_at) VALUES (?, {self._placeholder('json')}, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (user_id, json.dumps(data, ensure_ascii=False),
             self._to_db("datetime", datetime.now(timezone.utc))),
        )
        self.connection.commit()

    def delete_draft(self, user_id: int) -> None:
        self._execute("DELETE FROM drafts WHERE user_id = ?", (user_id,))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class SqliteIssueRepository(_SqlIssueRepository):
    """Лёгкий backend для тестов и локального запуска без Docker."""

    def __init__(self, database_path: str) -> None:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()


class PostgresIssueRepository(_SqlIssueRepository):
    """Основное хранилище итоговой версии приложения."""

    ph = "%s"
    postgres = True

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "Для запуска итоговой версии установите зависимости из requirements.txt"
            ) from exc
        self.connection = psycopg.connect(database_url, row_factory=dict_row)
        self._create_schema()

    def list_for_user(self, user_id: int) -> list[Issue]:
        rows = self._execute(
            "SELECT * FROM issues WHERE participants @> ?::jsonb ORDER BY updated_at DESC",
            (json.dumps([user_id]),),
        ).fetchall()
        return [_from_row(Issue, dict(row)) for row in rows]
