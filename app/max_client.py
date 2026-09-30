from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class MaxApiError(RuntimeError):
    pass


class MaxBotClient:
    def __init__(self, token: str, base_url: str, timeout: int = 40) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def aclose(self) -> None:
        return None

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def set_commands(self, commands: list[dict[str, str]]) -> dict[str, Any]:
        """Меню команд бота (PATCH /me/commands)."""
        return await self._request("PATCH", "/me/commands", json={"commands": commands})

    async def get_updates(
        self, *, marker: int | None, timeout: int
    ) -> tuple[list[dict[str, Any]], int | None]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "types": "message_created,message_callback,bot_started",
        }
        if marker is not None:
            params["marker"] = marker
        payload = await self._request("GET", "/updates", params=params)
        if isinstance(payload, list):
            return payload, None
        next_marker = payload.get("marker")
        return payload.get("updates", []), int(next_marker) if next_marker is not None else None

    async def send_message(
        self,
        user_id: int,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return await self._post_message({"user_id": user_id}, text, attachments)

    async def send_chat_message(
        self,
        chat_id: int,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Сообщение в групповой чат (чат дома)."""
        return await self._post_message({"chat_id": chat_id}, text, attachments)

    async def _post_message(
        self, params: dict[str, Any], text: str, attachments: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text[:4000]}
        if attachments:
            body["attachments"] = attachments
        return await self._request("POST", "/messages", params=params, json=body)

    async def answer_callback(
        self,
        callback_id: str,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Подтверждает нажатие кнопки; с text заменяет исходное сообщение новым."""
        body: dict[str, Any] = {}
        if text:
            body["message"] = {"text": text[:4000], "attachments": attachments or []}
        return await self._request(
            "POST",
            "/answers",
            params={"callback_id": callback_id},
            json=body,
        )

    async def upload_image(self, content: bytes, filename: str = "image.png") -> str:
        """Загружает картинку и возвращает токен для вложения type=image.

        Порядок из документации MAX: POST /uploads?type=image → url, затем
        multipart-загрузка файла в поле data.
        """
        slot = await self._request("POST", "/uploads", params={"type": "image"})
        upload_url = slot.get("url")
        if not upload_url:
            raise MaxApiError(f"MAX не выдал адрес загрузки: {slot}")
        result = await asyncio.to_thread(self._upload_sync, upload_url, content, filename)
        token = self._extract_token(result) or slot.get("token")
        if not token:
            raise MaxApiError(f"MAX не вернул токен изображения: {result}")
        return str(token)

    @staticmethod
    def _extract_token(result: Any) -> str | None:
        if not isinstance(result, dict):
            return None
        if result.get("token"):
            return result["token"]
        photos = result.get("photos")
        if isinstance(photos, dict):
            for photo in photos.values():
                if isinstance(photo, dict) and photo.get("token"):
                    return photo["token"]
        return None

    def _upload_sync(self, url: str, content: bytes, filename: str) -> Any:
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="data"; filename="{filename}"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        request = Request(
            url=url,
            method="POST",
            headers={
                "Authorization": self._token,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            data=body,
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except Exception as exc:
            raise MaxApiError(f"MAX upload failed: {exc}") from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        data = await asyncio.to_thread(self._request_sync, method, path, kwargs)
        return data if isinstance(data, dict) else {"data": data}

    def _request_sync(
        self, method: str, path: str, kwargs: dict[str, Any]
    ) -> dict[str, Any] | list[Any]:
        params = kwargs.get("params") or {}
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        body = kwargs.get("json")
        request = Request(
            url=url,
            method=method,
            headers={
                "Authorization": self._token,
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode("utf-8") if body is not None else None,
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = response.read().decode("utf-8")
                decoded = json.loads(payload) if payload else {}
                return decoded
        except Exception as exc:
            raise MaxApiError(f"MAX API request failed: {exc}") from exc
