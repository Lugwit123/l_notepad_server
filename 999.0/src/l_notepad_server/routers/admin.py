# -*- coding: utf-8 -*-
"""管理员 API + 页面：/admin/notes、/api/admin/*（admin / system 角色）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import auth as authmod
from .. import file_store
from .. import note_access
from .deps import get_conn, get_notes_root, get_templates, require_admin, template_ctx

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


class OwnerRequest(BaseModel):
    owner: str


class PublicRequest(BaseModel):
    public: bool = True


@router.get("/admin/notes", response_class=HTMLResponse)
def admin_notes_page(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(request, "admin_notes.html", {**template_ctx(request)})


@router.get("/api/admin/notes")
def admin_notes_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"notes": note_access.list_all_notes(conn)}


@router.get("/api/admin/users")
def admin_users_list(request: Request) -> dict[str, Any]:
    """通过 Auth Service 返回全部用户列表（改拥有者下拉用）"""
    token = str(getattr(request.state, "login_token", "") or "")
    return {"users": authmod.list_users(token)}


@router.put("/api/admin/notes/{note_path:path}/owner")
def admin_note_set_owner(
    note_path: str,
    payload: OwnerRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not note_access.is_registered(conn, note_path):
        raise HTTPException(status_code=404, detail="Note not found")
    note_access.set_owner(conn, note_path, payload.owner)
    return {"ok": True, "owner": payload.owner}


@router.post("/api/admin/notes/{note_path:path}/public")
def admin_note_set_public(
    note_path: str,
    payload: PublicRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not note_access.is_registered(conn, note_path):
        raise HTTPException(status_code=404, detail="Note not found")
    note_access.set_public(conn, note_path, bool(payload.public), "read")
    return {"ok": True, "public": bool(payload.public)}


@router.delete("/api/admin/notes/{note_path:path}")
def admin_note_delete(
    note_path: str,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    if not file_store.delete_note(notes_root, note_path):
        raise HTTPException(status_code=404, detail="Note not found")
    conn.execute("DELETE FROM note_registry WHERE note_path = ?", (note_path,))
    conn.execute("DELETE FROM note_shares WHERE note_path = ?", (note_path,))
    conn.execute("DELETE FROM note_tags WHERE note_path = ?", (note_path,))
    note_access.delete_note_forks(conn, note_path)
    conn.commit()
    return {"ok": True}
