# -*- coding: utf-8 -*-
"""笔记 REST API：/api/notes*（CRUD + 共享管理）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import file_store
from .. import note_access
from .deps import current_user, get_conn, get_notes_root, is_admin

router = APIRouter(prefix="/api/notes", tags=["notes"])


class NoteCreate(BaseModel):
    title: str = Field(default="未命名", max_length=200)
    content: str = Field(default="")
    category: str = Field(default="", description="optional directory path under notepad_list")


class NoteUpdate(BaseModel):
    title: str = Field(default="未命名", max_length=200)
    content: str = Field(default="")


class ShareRequest(BaseModel):
    username: str
    permission: str = "read"


class TagRequest(BaseModel):
    tag: str


class MoveRequest(BaseModel):
    dst_dir: str = ""


class ForkRequest(BaseModel):
    title: str = Field(default="", max_length=200)


class NoteOut(BaseModel):
    path: str
    title: str
    content: str
    created_at: str
    updated_at: str
    is_md: bool = False

    @staticmethod
    def from_file_note(note: file_store.FileNote, *, include_content: bool = True) -> "NoteOut":
        return NoteOut(
            path=note.path,
            title=note.title,
            content=note.content if include_content else note.content_snippet(),
            created_at=note.created_at,
            updated_at=note.updated_at,
            is_md=note.is_markdown,
        )


def _note_or_404(notes_root: Path, note_path: str) -> file_store.FileNote:
    note = file_store.get_note(notes_root, note_path)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.get("", response_model=list[NoteOut])
def list_notes(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
    limit: int = 200,
) -> list[NoteOut]:
    from .deps import accessible_brief

    notes = accessible_brief(request, conn, limit=limit)
    return [NoteOut.from_file_note(n, include_content=False) for n in notes]


@router.post("", response_model=NoteOut)
def create_note(
    request: Request,
    payload: NoteCreate,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> NoteOut:
    user = current_user(request)
    try:
        note = file_store.create_note(notes_root, title=payload.title, content=payload.content, category_dir=payload.category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    note_access.register_note(conn, note.path, user)
    return NoteOut.from_file_note(note, include_content=True)


def _require_owner_or_admin(request: Request, conn: sqlite3.Connection, note_path: str) -> None:
    if note_access.can_access(conn, note_path, current_user(request)) != "owner" and not is_admin(request):
        raise HTTPException(status_code=403, detail="仅拥有者或管理员可管理共享")


def _require_editable(request: Request, conn: sqlite3.Connection, note_path: str) -> None:
    if not note_access.can_edit(conn, note_path, current_user(request)):
        raise HTTPException(status_code=403, detail="无编辑权限")


@router.get("/{note_path:path}/shares")
def note_shares_list(
    request: Request,
    note_path: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    _require_owner_or_admin(request, conn, note_path)
    return {"note_path": note_path, "shares": note_access.list_shares(conn, note_path)}


@router.post("/{note_path:path}/share")
def note_share_add(
    request: Request,
    note_path: str,
    payload: ShareRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    _require_owner_or_admin(request, conn, note_path)
    note_access.share_note(conn, note_path, payload.username, payload.permission)
    return {"ok": True, "shares": note_access.list_shares(conn, note_path)}


@router.delete("/{note_path:path}/share/{username}")
def note_share_remove(
    request: Request,
    note_path: str,
    username: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    _require_owner_or_admin(request, conn, note_path)
    note_access.unshare_note(conn, note_path, username)
    return {"ok": True, "shares": note_access.list_shares(conn, note_path)}


# ── 标签（owner / 可编辑共享者可管理）────────────────────


@router.get("/{note_path:path}/tags")
def note_tags_list(
    request: Request,
    note_path: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not note_access.can_access(conn, note_path, current_user(request), admin=is_admin(request)):
        raise HTTPException(status_code=404, detail="Note not found")
    return {"note_path": note_path, "tags": note_access.list_tags(conn, note_path)}


@router.post("/{note_path:path}/tags")
def note_tags_add(
    request: Request,
    note_path: str,
    payload: TagRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    _require_editable(request, conn, note_path)
    if not note_access.add_tag(conn, note_path, payload.tag):
        raise HTTPException(status_code=400, detail="无效标签")
    return {"ok": True, "tags": note_access.list_tags(conn, note_path)}


@router.delete("/{note_path:path}/tags/{tag}")
def note_tags_remove(
    request: Request,
    note_path: str,
    tag: str,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    _require_editable(request, conn, note_path)
    note_access.remove_tag(conn, note_path, tag)
    return {"ok": True, "tags": note_access.list_tags(conn, note_path)}


# ── 移动分组（仅拥有者）─────────────────────────────────


@router.post("/{note_path:path}/move")
def note_move(
    request: Request,
    note_path: str,
    payload: MoveRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    if note_access.can_access(conn, note_path, current_user(request)) != "owner":
        raise HTTPException(status_code=403, detail="仅拥有者可移动")
    try:
        note = file_store.move_note(notes_root, note_path, payload.dst_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.path != note_path:
        note_access.migrate_note_path(conn, note_path, note.path)
    return {"ok": True, "path": note.path}


# ── fork：把一篇可访问笔记复制为当前用户自己的笔记，并记录溯源关系 ──


@router.post("/{note_path:path}/fork", response_model=NoteOut)
def note_fork(
    request: Request,
    note_path: str,
    payload: ForkRequest,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> NoteOut:
    user = current_user(request)
    if not note_access.can_access(conn, note_path, user, admin=is_admin(request)):
        raise HTTPException(status_code=404, detail="Note not found")
    src = _note_or_404(notes_root, note_path)
    title = (payload.title or "").strip() or src.title
    try:
        note = file_store.create_note(notes_root, title=title, content=src.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    note_access.register_note(conn, note.path, user)
    note_access.register_fork(conn, note.path, note_path)
    return NoteOut.from_file_note(note, include_content=True)


@router.get("/{note_path:path}/baidu_link")
def note_baidu_link(
    request: Request,
    note_path: str,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    """返回该笔记在 lugwit_baidu_netdisk 网页端的文件浏览地址（「百度云地址」）。

    云端不可用/未配置时 ok=false, url 为空；不阻塞页面渲染。
    """
    user = current_user(request)
    if not note_access.can_access(conn, note_path, user, admin=is_admin(request)):
        raise HTTPException(status_code=404, detail="Note not found")
    _note_or_404(notes_root, note_path)
    try:
        from .. import cloud_sync

        link = cloud_sync.note_browser_link(note_path, request=request)
    except Exception:
        link = {"url": "", "remote_path": "", "folder": ""}
    return {"ok": bool(link.get("url")), **link}


@router.get("/{note_path:path}", response_model=NoteOut)
def get_note(
    request: Request,
    note_path: str,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> NoteOut:
    if not note_access.can_access(conn, note_path, current_user(request), admin=is_admin(request)):
        raise HTTPException(status_code=403, detail="无权访问该笔记")
    note = _note_or_404(notes_root, note_path)
    return NoteOut.from_file_note(note, include_content=True)


@router.put("/{note_path:path}", response_model=NoteOut)
def update_note(
    request: Request,
    note_path: str,
    payload: NoteUpdate,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> NoteOut:
    if not note_access.can_edit(conn, note_path, current_user(request)):
        raise HTTPException(status_code=403, detail="无编辑权限")
    try:
        note = file_store.update_note(notes_root, note_path, new_title=payload.title, new_content=payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.path != note_path:
        # 文件改名 → 同步注册表 / 共享关系路径
        note_access.migrate_note_path(conn, note_path, note.path)
    return NoteOut.from_file_note(note, include_content=True)


@router.delete("/{note_path:path}")
def delete_note(
    request: Request,
    note_path: str,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    if note_access.can_access(conn, note_path, current_user(request)) != "owner":
        raise HTTPException(status_code=403, detail="仅拥有者可删除")
    if not file_store.delete_note(notes_root, note_path):
        raise HTTPException(status_code=404, detail="Note not found")
    conn.execute("DELETE FROM note_registry WHERE note_path = ?", (note_path,))
    conn.execute("DELETE FROM note_shares WHERE note_path = ?", (note_path,))
    conn.execute("DELETE FROM note_tags WHERE note_path = ?", (note_path,))
    note_access.delete_note_forks(conn, note_path)
    from .. import knowledge as kbmod
    kbmod.delete_note_cleanup(conn, note_path)
    conn.commit()
    return {"ok": True}


# ── 元信息 API：分组 / 标签云 / 创建者（登录即可）──────────

meta_router = APIRouter(prefix="/api", tags=["meta"])


@meta_router.get("/groups")
def api_groups(notes_root: Path = Depends(get_notes_root)) -> list[dict[str, Any]]:
    return [{"path": g, "count": c} for g, c in file_store.list_group_dirs(notes_root)]


@meta_router.get("/tags")
def api_tags(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    """标签云（仅统计当前用户可见的笔记）。"""
    accessible = note_access.list_accessible(conn, current_user(request))
    counter: dict[str, int] = {}
    for path, tag in note_access.all_tag_pairs(conn):
        if path in accessible:
            counter[tag] = counter.get(tag, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counter.items())]


@meta_router.get("/creators")
def api_creators(conn: sqlite3.Connection = Depends(get_conn)) -> list[str]:
    return note_access.distinct_owners(conn)
