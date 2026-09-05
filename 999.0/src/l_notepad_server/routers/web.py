# -*- coding: utf-8 -*-
"""网页端：/ 登录后的页面路由 + 登录/登出 API + 服务器日志查看页。"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .. import auth as authmod
from .. import file_store
from .. import note_access
from .deps import (
    accessible_brief,
    current_user,
    get_conn,
    get_templates,
    is_admin,
    mounted_url,
    template_ctx,
)

router = APIRouter(tags=["web"])

# 全文搜索时单文件读取上限
_SEARCH_MAX_BYTES = 2 * 1024 * 1024

_TAG_SPLIT = re.compile(r"[,，;；]+")


def _parse_tags(raw: Any) -> list[str]:
    return [p.strip() for p in _TAG_SPLIT.split(str(raw or "")) if p.strip()]


def _normalize_content(raw: Any) -> str:
    """表单文本归一：CRLF/CR → LF（Windows 表单提交带 \r\n）。"""
    return str(raw or "").replace("\r\n", "\n").replace("\r", "\n")


def _tag_cloud(conn: sqlite3.Connection, accessible: set[str]) -> list[dict[str, Any]]:
    """可见笔记的标签云 [{tag, count}]。"""
    counter: dict[str, int] = {}
    for path, tag in note_access.all_tag_pairs(conn):
        if path in accessible:
            counter[tag] = counter.get(tag, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counter.items())]


def _groups_from_notes(notes: list[file_store.FileNote]) -> list[tuple[str, int]]:
    """可见笔记按一级目录聚合计数（分组树）。"""
    groups: dict[str, int] = {}
    for n in notes:
        parts = n.path.split("/")
        if len(parts) > 1:
            g = "/".join(parts[:-1])
            groups[g] = groups.get(g, 0) + 1
    return sorted(groups.items())


class LoginRequest(BaseModel):
    username: str
    password: str


# ── 登录 / 登出（认证走 Auth Service；cookie 由服务端设置，HttpOnly 防 XSS 窃取）──


@router.post("/api/auth/login")
async def auth_login(payload: LoginRequest, response: Response) -> dict[str, Any]:
    data = await authmod.login(payload.username, payload.password)
    if not data:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    response.set_cookie(
        "l_notepad_token",
        data["access_token"],
        max_age=7 * 24 * 3600,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return {"access_token": data["access_token"], "token_type": "bearer", "user": data["user"]}


@router.post("/api/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie("l_notepad_token", path="/")
    return {"ok": True}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(request, "login.html", {**template_ctx(request)})


# ── 状态页 ──


@router.get("/", response_class=HTMLResponse)
def index(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
    templates = get_templates(request)
    notes = accessible_brief(request, conn, limit=200)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"notes": notes, "owner_map": note_access.owner_map(conn), **template_ctx(request)},
    )


# ── 服务器日志查看页（管理员）── 注意：必须在 /web/{note_path:path} 之前注册


@router.get("/web/logs", response_class=HTMLResponse)
def web_logs_page(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(request, "web_logs.html", {**template_ctx(request)})


# ── 笔记列表 / 新建 / 编辑 ──


@router.get("/web", response_class=HTMLResponse)
def web_list(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    q: str = "",
) -> HTMLResponse:
    templates = get_templates(request)
    notes = accessible_brief(request, conn, limit=500)
    visible = {n.path for n in notes}
    tags_map: dict[str, list[str]] = {}
    for path, tag in note_access.all_tag_pairs(conn):
        if path in visible:
            tags_map.setdefault(path, []).append(tag)
    owner_map = note_access.owner_map(conn)
    creators = sorted({owner_map[n.path] for n in notes if owner_map.get(n.path)})
    groups = _groups_from_notes(notes)
    query = (q or "").strip().lower()
    if query:
        # 服务端全文搜索：摘要（8KB 头）匹配不到时回源读全文（单文件上限 2MB）
        matched: list[file_store.FileNote] = []
        for n in notes:
            if query in n.title.lower() or query in n.content.lower():
                matched.append(n)
                continue
            p = Path(request.app.state.notes_root) / n.path
            try:
                if query in file_store.read_text_capped(p, _SEARCH_MAX_BYTES).lower():
                    matched.append(n)
            except OSError:
                continue
        notes = matched
    return templates.TemplateResponse(
        request,
        "web_list.html",
        {
            "notes": notes,
            "q": q,
            "active_note_path": None,
            "owned_paths": note_access.list_owned_by(conn, current_user(request)),
            "owner_map": owner_map,
            "groups": groups,
            "all_tags": _tag_cloud(conn, visible),
            "tags_map": tags_map,
            "creators": creators,
            **template_ctx(request),
        },
    )


@router.get("/web/new", response_class=HTMLResponse)
def web_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
    templates = get_templates(request)
    notes_root: Path = request.app.state.notes_root
    accessible = note_access.list_accessible(conn, current_user(request), admin=is_admin(request))
    return templates.TemplateResponse(
        request,
        "web_edit.html",
        {
            "note": None,
            "mode": "new",
            "notes": accessible_brief(request, conn, limit=500),
            "active_note_path": None,
            "can_edit": True,
            "owner_map": note_access.owner_map(conn),
            "groups": file_store.list_group_dirs(notes_root),
            "all_tags": _tag_cloud(conn, accessible),
            "note_tags": [],
            **template_ctx(request)},
    )


@router.post("/web/new")
async def web_new_post(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> RedirectResponse:
    form = await request.form()
    title = str(form.get("title", "")).strip() or "未命名"
    content = _normalize_content(form.get("content", ""))
    group = str(form.get("new_group") or form.get("group") or "").strip()
    user = current_user(request)
    notes_root: Path = request.app.state.notes_root
    try:
        note = file_store.create_note(notes_root, title=title, content=content, category_dir=group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    note_access.register_note(conn, note.path, user)
    note_access.set_tags(conn, note.path, _parse_tags(form.get("tags")))
    return RedirectResponse(url=mounted_url(request, f"web/{note.path}"), status_code=303)


@router.get("/web/{note_path:path}", response_class=HTMLResponse)
def web_edit(
    note_path: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    templates = get_templates(request)
    user = current_user(request)
    if not note_access.can_access(conn, note_path, user, admin=is_admin(request)):
        raise HTTPException(status_code=404, detail="Note not found")
    notes_root: Path = request.app.state.notes_root
    note = file_store.get_note(notes_root, note_path)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    is_owner = note_access.can_access(conn, note_path, user) == "owner"
    access_level = note_access.can_access(conn, note_path, user, admin=is_admin(request))
    show_fork = access_level is not None and not is_owner
    fork_source = note_access.get_fork_source(conn, note_path)
    fork_source_owner = note_access.get_owner(conn, fork_source) if fork_source else None
    accessible = note_access.list_accessible(conn, user, admin=is_admin(request))
    return templates.TemplateResponse(
        request,
        "web_edit.html",
        {
            "note": note,
            "mode": "edit",
            "notes": accessible_brief(request, conn, limit=500),
            "active_note_path": note_path,
            "is_owner": is_owner,
            "can_edit": is_owner or note_access.can_edit(conn, note_path, user),
            "shares": note_access.list_shares(conn, note_path) if is_owner else [],
            "owner_name": note_access.get_owner(conn, note_path),
            "owner_map": note_access.owner_map(conn),
            "groups": file_store.list_group_dirs(notes_root),
            "all_tags": _tag_cloud(conn, accessible),
            "note_tags": note_access.list_tags(conn, note_path),
            "show_fork": show_fork,
            "fork_source": fork_source,
            "fork_source_owner": fork_source_owner,
            **template_ctx(request)},
    )


@router.post("/web/{note_path:path}/delete")
async def web_delete_post(
    note_path: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    if note_access.can_access(conn, note_path, current_user(request)) != "owner":
        raise HTTPException(status_code=403, detail="仅拥有者可删除")
    notes_root: Path = request.app.state.notes_root
    file_store.delete_note(notes_root, note_path)
    conn.execute("DELETE FROM note_registry WHERE note_path = ?", (note_path,))
    conn.execute("DELETE FROM note_shares WHERE note_path = ?", (note_path,))
    conn.execute("DELETE FROM note_tags WHERE note_path = ?", (note_path,))
    note_access.delete_note_forks(conn, note_path)
    from .. import knowledge as kbmod
    kbmod.delete_note_cleanup(conn, note_path)
    conn.commit()
    return RedirectResponse(url=mounted_url(request, "web"), status_code=303)


@router.post("/web/{note_path:path}")
async def web_edit_post(
    note_path: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> RedirectResponse:
    if not note_access.can_edit(conn, note_path, current_user(request)):
        raise HTTPException(status_code=403, detail="无编辑权限")
    form = await request.form()
    title = str(form.get("title", "")).strip() or "未命名"
    content = _normalize_content(form.get("content", ""))
    notes_root: Path = request.app.state.notes_root
    note = file_store.update_note(notes_root, note_path, new_title=title, new_content=content)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.path != note_path:
        # 文件改名 → 同步注册表 / 共享关系 / 标签路径
        note_access.migrate_note_path(conn, note_path, note.path)
    note_access.set_tags(conn, note.path, _parse_tags(form.get("tags")))
    return RedirectResponse(url=mounted_url(request, f"web/{note.path}"), status_code=303)
