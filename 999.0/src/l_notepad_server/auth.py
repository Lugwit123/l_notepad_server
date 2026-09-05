# -*- coding: utf-8 -*-
"""l_notepad 用户认证：仅通过 Auth Service（lugwit_auth 独立服务）HTTP 接入

架构：客户端不直连用户数据库、不直接依赖 lugwit_auth 包。
  - 登录 / 用户列表 / token 验证 → 全部走 Auth Service REST API
  - 本模块仅使用标准库 urllib 发起 HTTP 请求（异步登录用 to_thread）
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Optional

from pytracemp import lprint

from . import server_config

_TIMEOUT = 10
# 鉴权中间件每个请求都会 verify，必须用短超时，避免服务未就绪时卡住大量请求
_VERIFY_TIMEOUT = 3


def _http_json(method: str, path: str, body: Any = None, token: str = "", timeout: int = _TIMEOUT) -> Any:
    """向 Auth Service 发起 HTTP 请求，返回解析后的 JSON；失败抛异常"""
    url = server_config.auth_url().rstrip("/") + path
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


async def login(username: str, plain_password: str) -> Optional[dict]:
    """通过 Auth Service 登录，成功返回 {access_token, user}，失败返回 None"""
    try:
        return await asyncio.to_thread(
            _http_json, "POST", "/api/v1/auth/login",
            {"username": username, "password": plain_password},
        )
    except Exception:
        # Auth Service 不可用等异常统一视为登录失败，避免泄漏内部信息
        return None


def list_users(token: str) -> list[dict]:
    """通过 Auth Service 拉取全部用户（改 owner 下拉用），失败返回空列表"""
    try:
        result = _http_json("GET", "/api/v1/users", token=token, timeout=_VERIFY_TIMEOUT)
        return (result or {}).get("users", [])
    except Exception:
        return []


def verify_token(token: str) -> Optional[dict[str, Any]]:
    """通过 Auth Service 验证 JWT。

    返回 ``payload``（有效）或 ``None``（明确无效：HTTP 401 / valid=False）。
    当 Auth Service 不可用（连接失败/超时）时**抛出异常**，由调用方决定是否重试——
    避免把「服务未就绪」误判成「token 失效」而清除本地登录（导致每次都要重新登录）。
    """
    try:
        payload = _http_json("POST", "/api/v1/auth/verify", token=token, timeout=_VERIFY_TIMEOUT)
    except urllib.error.HTTPError:
        # 4xx/5xx：token 无效或服务端明确拒绝 → 视为无效
        return None
    except Exception as exc:
        # 连接失败/超时等服务不可用 → 抛出让调用方区分
        lprint(f"[l_notepad][verify] Auth 服务不可用: {exc}")
        raise
    if not payload or not payload.get("valid"):
        lprint(f"[l_notepad][verify] 无效响应: {payload}")
        return None
    return payload.get("payload")


def role_int_to_label(role_int: Any) -> str:
    return {0: "用户", 1: "管理员", 2: "系统"}.get(role_int, "")


__all__ = ["login", "list_users", "verify_token", "role_int_to_label"]
