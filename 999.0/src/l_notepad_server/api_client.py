# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class NoteDto:
    id: int
    title: str
    content: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LogDto:
    """Server log file metadata."""
    path: str       # relative posix path
    size: int       # file size in bytes
    mtime: str      # ISO timestamp


def _read_json(resp) -> Any:
    raw = resp.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


class NotepadApi:
    def __init__(self, base_url: str, token: str | None = None,
                 auth_url: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        # 账号/收藏数据走认证服务：url = auth_url + "/api/v1/..."
        # （auth_url 默认是 nginx 8080 入口，location /api/v1/ 转发到认证服务），
        # 不依赖笔记后端 8765（8765 未启动时账号页/云同步也能用）。
        self.auth_url = (auth_url or "").rstrip("/") or None

    def health(self) -> bool:
        data = self._get("/api/health")
        return bool(data and data.get("ok"))

    def list_notes(self) -> list[NoteDto]:
        data = self._get("/api/notes")
        return [NoteDto(**x) for x in (data or [])]

    def get_note(self, note_id: int) -> NoteDto:
        data = self._get(f"/api/notes/{note_id}")
        return NoteDto(**data)

    def create_note(self, title: str, content: str) -> NoteDto:
        data = self._post("/api/notes", {"title": title, "content": content})
        return NoteDto(**data)

    def update_note(self, note_id: int, title: str, content: str) -> NoteDto:
        data = self._put(f"/api/notes/{note_id}", {"title": title, "content": content})
        return NoteDto(**data)

    def delete_note(self, note_id: int) -> None:
        self._delete(f"/api/notes/{note_id}")

    def list_logs(self) -> list[LogDto]:
        data = self._get("/api/logs")
        return [LogDto(**x) for x in (data or [])]

    def get_log(self, log_path: str) -> dict[str, str]:
        """Fetch server log content. Returns {'path': ..., 'content': ...}."""
        return self._get(f"/api/logs/{log_path}")

    def update_log(self, log_path: str, content: str) -> None:
        """Update (overwrite) a server log file."""
        self._put(f"/api/logs/{log_path}", {"title": "", "content": content})

    def delete_log(self, log_path: str) -> None:
        """Delete a server log file."""
        self._delete(f"/api/logs/{log_path}")

    # ── 账号收藏（走认证服务；无 auth_url 时回退笔记后端 base_url） ──
    def _account_path(self, sub: str) -> str:
        """账号/收藏接口路径：走认证服务（auth_url + /api/v1）或笔记后端（base_url + /api）。"""
        prefix = "/api/v1" if self.auth_url else "/api"
        return f"{prefix}{sub}"

    @staticmethod
    def _unwrap(data, key: str, default):
        """认证服务返回 {key: value} 包装（如 {"accounts": [...]}）；
        笔记后端转发时已解包直接返回。"""
        if isinstance(data, dict) and key in data:
            return data.get(key, default)
        return data if data is not None else default

    def list_accounts(self) -> list[dict[str, Any]]:
        """账号列表（password/自定义字段为明文，由服务端解密返回）"""
        data = self._request("GET", self._account_path("/accounts"), None, auth=True)
        return self._unwrap(data, "accounts", []) or []

    def add_account(self, data: dict[str, Any]) -> dict[str, Any]:
        resp = self._request("POST", self._account_path("/accounts"), data, auth=True)
        return self._unwrap(resp, "account", {})

    def update_account(self, account_id: int, data: dict[str, Any]) -> dict[str, Any]:
        resp = self._request(
            "PUT", self._account_path(f"/accounts/{account_id}"), data, auth=True)
        return self._unwrap(resp, "account", {})

    def delete_account(self, account_id: int) -> None:
        self._request("DELETE", self._account_path(f"/accounts/{account_id}"), None, auth=True)

    def get_account_custom_fields(self) -> list[str]:
        data = self._request(
            "GET", self._account_path("/accounts/custom-fields"), None, auth=True)
        return self._unwrap(data, "names", []) or []

    def save_account_custom_fields(self, names: list[str]) -> None:
        self._request(
            "PUT", self._account_path("/accounts/custom-fields"),
            {"names": names}, auth=True)

    # ── 通用收藏项（文件夹/命令/网址 云同步，走认证服务） ──
    def list_fav_items(self, kind: str = "") -> list[dict[str, Any]]:
        path = self._account_path("/fav-items")
        if kind:
            path += f"?kind={urllib.parse.quote(kind)}"
        data = self._request("GET", path, None, auth=True)
        return self._unwrap(data, "items", []) or []

    def add_fav_item(self, kind: str, name: str, value: str) -> dict[str, Any]:
        resp = self._request(
            "POST", self._account_path("/fav-items"),
            {"kind": kind, "name": name, "value": value}, auth=True)
        return self._unwrap(resp, "item", {})

    def update_fav_item(self, item_id: int, kind: str, name: str, value: str) -> dict[str, Any]:
        resp = self._request(
            "PUT", self._account_path(f"/fav-items/{item_id}"),
            {"kind": kind, "name": name, "value": value}, auth=True)
        return self._unwrap(resp, "item", {})

    def delete_fav_item(self, item_id: int) -> None:
        self._request("DELETE", self._account_path(f"/fav-items/{item_id}"), None, auth=True)

    def _get(self, path: str) -> Any:
        return self._request("GET", path, None)

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", path, payload)

    def _put(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("PUT", path, payload)

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path, None)

    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None, *, auth: bool = False) -> Any:
        base = self.auth_url if (auth and self.auth_url) else self.base_url
        url = f"{base}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        token = getattr(self, "token", None)
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return _read_json(resp)
        except urllib.error.HTTPError as e:
            body = e.read()
            msg = body.decode("utf-8", errors="ignore") if body else str(e)
            raise ApiError(f"{method} {url} failed: {e.code} {msg}") from e
        except Exception as e:
            raise ApiError(f"{method} {url} failed: {e}") from e

