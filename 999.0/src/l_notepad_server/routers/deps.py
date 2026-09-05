# -*- coding: utf-8 -*-
"""FastAPI 公共依赖：请求级 DB 连接、登录态、权限、模板上下文、URL 前缀。

参考 FastAPI full-stack template 的模式：路由不再通过闭包捕获 app 状态，
全部通过 Depends 注入。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException, Request

from .. import db as dbmod
from .. import file_store
from .. import note_access


def get_db_path(request: Request) -> Path:
    return request.app.state.db_path


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """每请求独立 SQLite 连接（WAL 支持并发读），请求结束关闭。"""
    conn = dbmod.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_notes_root(request: Request) -> Path:
    """请求时读取（而非启动时闭包捕获），保证 app.state.notes_root 可被测试覆写。"""
    return request.app.state.notes_root


def get_templates(request: Request) -> Any:
    return request.app.state.templates


def current_user(request: Request) -> str:
    return str(getattr(request.state, "login_user", "") or "guest")


def is_admin(request: Request) -> bool:
    return getattr(request.state, "login_role_int", None) in (1, 2)


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="需要管理员权限")


def login_ctx(request: Request) -> dict[str, Any]:
    """模板渲染用的登录上下文。"""
    return {
        "login_user": getattr(request.state, "login_user", None),
        "login_role": getattr(request.state, "login_role", ""),
        "login_user_id": getattr(request.state, "login_user_id", None),
        "is_admin": is_admin(request),
    }


# ── URL 前缀（nginx 反代剥前缀场景）─────────────────────────


def root_base(request: Request) -> str:
    """URL 前缀：优先 ASGI root_path（mount 子应用场景），
    其次 X-Forwarded-Prefix 头（nginx 剥前缀反代 /note/ 场景）。"""
    base = (request.scope.get("root_path") or "").rstrip("/")
    if base:
        return base
    return (request.headers.get("X-Forwarded-Prefix") or "").strip().rstrip("/")


def web_base(request: Request) -> str:
    rb = root_base(request)
    return f"{rb}/web" if rb else "/web"


def mounted_url(request: Request, path: str) -> str:
    path = path.lstrip("/")
    rb = root_base(request)
    return f"{rb}/{path}" if rb else f"/{path}"


def template_ctx(request: Request) -> dict[str, Any]:
    """所有模板共享的上下文（前缀 + 登录态 + helper）。"""
    return {
        "root_base": root_base(request),
        "web_base": web_base(request),
        "_mounted_url": mounted_url,
        **login_ctx(request),
    }


def accessible_brief(request: Request, conn: sqlite3.Connection, limit: int = 500) -> list[file_store.FileNote]:
    """当前用户可访问的笔记（拥有 + 共享 + 公共；管理员全可见），按更新时间倒序。"""
    user = current_user(request)
    accessible = note_access.list_accessible(conn, user, admin=is_admin(request))
    result = [
        n for n in file_store.list_notes_brief_cached(request.app.state.notes_root, limit=limit)
        if n.path in accessible
    ]
    result.sort(key=lambda n: n.updated_at, reverse=True)
    return result
