# -*- coding: utf-8 -*-
"""知识库：多知识库，每个知识库有独立路由 /web/kb/{name}，内含目录层级与多篇文章。

- 网页端：/web/kb（知识库总览）、/web/kb/{name}（某知识库）
- REST：/api/kb/bases*（知识库 CRUD）、/api/kb/{name}/...（某知识库的分类/文章）
- 兼容旧接口：/api/kb/categories、/api/kb/publish 等仍可用，归入默认知识库。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import depot_map
from .. import file_store
from .. import knowledge as kb
from .. import note_access
from .. import search_index
from .. import workspace_sync
from .deps import (
    current_user,
    get_conn,
    get_notes_root,
    get_templates,
    is_admin,
    template_ctx,
    web_base,
)

router = APIRouter(tags=["kb"])

log = logging.getLogger("l_notepad.kb")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PublishRequest(BaseModel):
    note_path: str
    category: str = Field(default="", description="目标层级路径，可多级用 / 分隔")
    is_public: bool = True


class CategoryRequest(BaseModel):
    path: str


class CategoryRename(BaseModel):
    old: str
    new: str


class BaseCreate(BaseModel):
    name: str
    title: str = ""
    description: str = ""


class WorkspaceRequest(BaseModel):
    workspace: str = ""


def _require_note_access_or_admin(request: Request, conn: sqlite3.Connection, note_path: str) -> None:
    if not note_access.can_access(conn, note_path, current_user(request), admin=is_admin(request)):
        raise HTTPException(status_code=404, detail="Note not found")


def _category_tree(paths: list[str]) -> list[dict[str, Any]]:
    """把扁平层级路径组装成树 [{path, children}]（预设层级目录树）。"""
    root: dict[str, Any] = {}
    for p in paths:
        node = root
        for seg in p.split("/"):
            node = node.setdefault(seg, {})
    nodes: list[dict[str, Any]] = []

    def build(node: dict, prefix: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for seg, children in node.items():
            full = f"{prefix}/{seg}" if prefix else seg
            out.append({"path": full, "children": build(children, full)})
        return out

    return build(root, "")


_WORKSPACE_EXTS = {".md", ".markdown", ".txt", ".rst", ".log"}


def _workspace_base(conn: sqlite3.Connection, kb_name: str) -> Optional[Path]:
    base = kb.get_base(conn, kb_name)
    if not base:
        raise HTTPException(status_code=404, detail="知识库不存在")
    ws = (base.get("workspace") or "").strip()
    return Path(ws) if ws else None


def _safe_ws_path(root: Path, rel: str) -> Path:
    """把工作区相对路径解析为绝对路径，防路径穿越（限制在 root 内）。"""
    r = root.resolve()
    p = (root / rel).resolve()
    if p != r and r not in p.parents:
        raise HTTPException(status_code=400, detail="路径越界")
    return p


def _scan_workspace(root: Path) -> list[dict[str, Any]]:
    """递归列出工作区内的可预览文本文件（.md/.txt/.rst/.log 等）。"""
    files: list[dict[str, Any]] = []
    if not root.is_dir():
        return files
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in _WORKSPACE_EXTS:
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            try:
                st = full.stat()
            except OSError:
                continue
            files.append({"rel": rel, "name": name, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda f: f["rel"].lower())
    return files


def _copy_note_to_workspace(
    conn: sqlite3.Connection, kb_name: str, note_path: str, content: str, category: str
) -> str:
    """把个人笔记内容复制为知识库工作区文件（按层级目录），返回工作区相对路径；未配置/失败返回 ''。

    这是「分享个人笔记到知识库」的第一步：自动复制笔记到工作区。
    """
    root = _workspace_base(conn, kb_name)
    if root is None or not root.is_dir():
        return ""
    cat = kb.normalize_path(category)
    ws_rel = (cat + "/" if cat else "") + Path(note_path).name
    try:
        target = _safe_ws_path(root, ws_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        log.warning("复制笔记到工作区失败 %s/%s: %s", kb_name, ws_rel, exc)
        return ""
    return ws_rel


def _depot_base_url() -> str:
    return depot_map.base_url()


def _depot_error(exc: Exception) -> HTTPException:
    detail = str(exc)
    status = 404 if "不存在" in detail else 502
    return HTTPException(status_code=status, detail=detail)


def kb_depot_mapping(conn: sqlite3.Connection, kb_name: str) -> dict[str, Any]:
    """知识库 depot 归档映射（库 / 子路径 / 工作区名 / 逻辑基路径）。"""
    try:
        return depot_map.mapping(conn, kb_name)
    except depot_map.DepotError as exc:
        raise _depot_error(exc) from exc


def kb_depot_workspace(conn: sqlite3.Connection, kb_name: str) -> dict[str, Any]:
    """确保知识库专属 depot 工作区存在并登记子路径映射。"""
    try:
        return depot_map.ensure_workspace(conn, kb_name)
    except depot_map.DepotError as exc:
        raise _depot_error(exc) from exc


def _depot_upload_content(conn: sqlite3.Connection, kb_name: str, ws_rel: str,
                          content: str, note_path: str = "") -> bool:
    """把工作区文件提交到 depot（`{library}/{subpath}/{rel}`）；失败只记日志。"""
    try:
        return depot_map.upload_content(conn, kb_name, ws_rel, content, note_path)
    except depot_map.DepotError as exc:
        log.warning("提交笔记到版本库失败 %s/%s: %s", kb_name, ws_rel, exc)
        return False


# ── 网页端 ──────────────────────────────────────────────


@router.get("/web/kb", response_class=HTMLResponse)
def kb_overview_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
    templates = get_templates(request)
    bases = kb.list_bases(conn)
    return templates.TemplateResponse(
        request,
        "web_kb_overview.html",
        {"kb_list": bases, **template_ctx(request)},
    )


@router.get("/web/kb/{kb_name}", response_class=HTMLResponse)
def kb_page(
    kb_name: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> HTMLResponse:
    templates = get_templates(request)
    base = kb.get_base(conn, kb_name)
    if not base:
        raise HTTPException(status_code=404, detail="知识库不存在")
    try:
        depot_base = depot_map.mapping(conn, kb_name)["base_path"]
    except depot_map.DepotError:
        depot_base = f"/notes/{kb_name}"
    return templates.TemplateResponse(
        request,
        "web_kb.html",
        {
            "kb": base,
            "depot_base": depot_base,
            "kb_tree": _category_tree(kb.list_categories(conn, kb_name)),
            **template_ctx(request),
        },
    )


# ── REST：知识库 CRUD（/api/kb/bases）───────────────────


class DepotMappingRequest(BaseModel):
    library: Optional[str] = None   # None=不改；""=回到默认 /notes
    subpath: Optional[str] = None   # None=不改；""=用知识库名
    ws_name: Optional[str] = None   # None=不改；""=kb-<知识库名>


@router.get("/api/kb/{kb_name}/depot")
def api_kb_depot(kb_name: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """知识库的版本库归档映射（库 / 子路径 / 工作区 / 本地目录 / 逻辑基路径）。"""
    return {"ok": True, **kb_depot_mapping(conn, kb_name)}


@router.put("/api/kb/{kb_name}/depot")
def api_kb_depot_save(
    kb_name: str,
    payload: DepotMappingRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """修改归档映射（库 / 库内子路径 / 工作区名）并重建工作区映射。"""
    try:
        info = depot_map.set_mapping(
            conn, kb_name,
            library=payload.library,
            subpath=payload.subpath,
            ws_name=payload.ws_name,
        )
    except depot_map.DepotError as exc:
        raise _depot_error(exc) from exc
    # 归档基路径变了 → 旧索引里的 rel 全部失效，后台按新映射重同步该库
    search_index.notify_kb_change(kb_name)
    workspace_sync.notify(kb_name)   # 归档基路径变了 → 下轮重新建基线
    return {"ok": True, **info}


@router.get("/api/kb/{kb_name}/depot/list")
def api_kb_depot_list(
    kb_name: str,
    conn: sqlite3.Connection = Depends(get_conn),
    rel: str = "",
    recursive: int = 0,
) -> dict[str, Any]:
    """列出知识库归档子路径下的内容（默认归档根；recursive=1 递归列全部文件）。"""
    try:
        if recursive:
            return {"ok": True, **depot_map.list_tree(conn, kb_name)}
        return {"ok": True, **depot_map.list_dir(conn, kb_name, rel=rel)}
    except depot_map.DepotError as exc:
        raise _depot_error(exc) from exc


@router.get("/api/kb/{kb_name}/depot/file")
def api_kb_depot_file(
    kb_name: str,
    conn: sqlite3.Connection = Depends(get_conn),
    rel: str = "",
    rev: int = 0,
) -> Response:
    """读取知识库归档文件（rev=0 最新），用于与本地/工作区内容比对。"""
    try:
        raw = depot_map.read_file(conn, kb_name, rel=rel, rev=rev)
    except depot_map.DepotError as exc:
        raise _depot_error(exc) from exc
    return Response(content=raw, media_type="text/plain; charset=utf-8")


@router.post("/api/kb/{kb_name}/depot/submit")
async def api_kb_depot_submit(
    kb_name: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    rel: str = "",
    description: str = "",
) -> dict[str, Any]:
    """把内容提交为知识库归档文件的新版本（body = 原始文本）。"""
    raw = await request.body()
    try:
        result = depot_map.submit_file(
            conn, kb_name, rel=rel, content=raw, description=description)
    except depot_map.DepotError as exc:
        raise _depot_error(exc) from exc
    search_index.notify_kb_change(kb_name)   # 归档变了 → 后台即时重索引该库
    return {"ok": True, **result}


@router.get("/api/kb/{kb_name}/search")
def api_kb_search(
    kb_name: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
    q: str = "",
    mode: str = "hybrid",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """在**单个知识库**范围内检索（倒排索引 + 语义），供知识库页面搜索框使用。

    作用域在 SQL 内完成（`search_docs.kb_name`），不需要前端拉全量再过滤。
    """
    if not kb.get_base(conn, kb_name):
        raise HTTPException(status_code=404, detail="知识库不存在")
    result = search_index.search(
        conn,
        notes_root,
        q,
        user=current_user(request),
        admin=is_admin(request),
        limit=limit,
        offset=offset,
        sources=["kb"],
        kb_name=kb_name,
        mode=(mode or "hybrid").strip().lower(),
    )
    hits = [
        {**h, "open_url": f"{web_base(request)}/kb/{urllib.parse.quote(kb_name)}"
                          f"?file={urllib.parse.quote(str(h.get('rel') or ''), safe='/')}"}
        for h in result["hits"]
    ]
    return {"kb": kb_name, "query": q, "mode": mode, "limit": limit, "offset": offset,
            **{**result, "hits": hits}}


@router.get("/api/kb/bases")
def api_kb_bases(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"bases": kb.list_bases(conn)}


@router.post("/api/kb/bases")
def api_kb_base_create(payload: BaseCreate, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not kb.create_base(conn, payload.name, payload.title, payload.description):
        raise HTTPException(status_code=400, detail="创建失败（名称为空或已存在）")
    return {"ok": True, "bases": kb.list_bases(conn)}


@router.delete("/api/kb/bases/{name}")
def api_kb_base_delete(name: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not kb.delete_base(conn, name):
        raise HTTPException(status_code=400, detail="删除失败（默认知识库不可删或不存在）")
    return {"ok": True, "bases": kb.list_bases(conn)}


# ── REST：某知识库的层级 / 文章 ───────────────────────────


@router.get("/api/kb/{kb_name}/categories")
def api_kb_categories(kb_name: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"categories": kb.list_categories(conn, kb_name)}


@router.post("/api/kb/{kb_name}/categories")
def api_kb_category_add(
    kb_name: str, payload: CategoryRequest, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    p = kb.ensure_category(conn, kb_name, payload.path)
    if not p:
        raise HTTPException(status_code=400, detail="空层级路径")
    return {"ok": True, "path": p, "categories": kb.list_categories(conn, kb_name)}


@router.delete("/api/kb/{kb_name}/categories/{path:path}")
def api_kb_category_del(kb_name: str, path: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not kb.delete_category(conn, kb_name, path):
        raise HTTPException(status_code=404, detail="层级不存在")
    return {"ok": True, "categories": kb.list_categories(conn, kb_name)}


@router.post("/api/kb/{kb_name}/categories/rename")
def api_kb_category_rename(
    kb_name: str, payload: CategoryRename, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    if not kb.rename_category(conn, kb_name, payload.old, payload.new):
        raise HTTPException(status_code=400, detail="重命名失败（路径为空或已相同）")
    return {"ok": True, "categories": kb.list_categories(conn, kb_name)}


@router.get("/api/kb/{kb_name}/articles")
def api_kb_articles(kb_name: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"articles": kb.list_articles(conn, kb_name)}


@router.post("/api/kb/{kb_name}/publish")
def api_kb_publish(
    kb_name: str,
    payload: PublishRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    """把笔记快照发布到指定知识库（需能访问该笔记）。"""
    if not kb.get_base(conn, kb_name):
        raise HTTPException(status_code=404, detail="知识库不存在")
    _require_note_access_or_admin(request, conn, payload.note_path)
    note = file_store.get_note(notes_root, payload.note_path)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    owner = note_access.get_owner(conn, payload.note_path) or current_user(request)
    ws_rel = _copy_note_to_workspace(conn, kb_name, payload.note_path, note.content, payload.category)
    kb.publish(
        conn,
        kb_name=kb_name,
        note_path=payload.note_path,
        title=note.title,
        content=note.content,
        category=payload.category,
        owner=owner,
        is_public=payload.is_public,
        workspace_rel=ws_rel,
    )
    if ws_rel:
        _depot_upload_content(conn, kb_name, ws_rel, note.content, payload.note_path)
        search_index.notify_kb_change(kb_name)   # 归档新增/更新 → 后台即时重索引
    return {"ok": True, "article": kb.get_article(conn, kb_name, payload.note_path)}


@router.post("/api/kb/{kb_name}/unpublish")
def api_kb_unpublish(kb_name: str, payload: PublishRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    kb.unpublish(conn, kb_name, payload.note_path)
    return {"ok": True}


@router.delete("/api/kb/{kb_name}/articles/{note_path:path}")
def api_kb_article_del(kb_name: str, note_path: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not kb.unpublish(conn, kb_name, note_path):
        raise HTTPException(status_code=404, detail="该笔记未发布到知识库")
    return {"ok": True}


# ── 工作区（本地目录预览 + 提交到百度云）──────────────────


@router.get("/api/kb/{kb_name}/workspace")
def api_kb_workspace(kb_name: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    base = kb.get_base(conn, kb_name)
    if not base:
        raise HTTPException(status_code=404, detail="知识库不存在")
    ws = (base.get("workspace") or "").strip()
    root = Path(ws) if ws else None
    exists = bool(root and root.is_dir())
    files = _scan_workspace(root) if (root and exists) else []
    shared = kb.list_shared_rels(conn, kb_name)
    for f in files:
        f["shared"] = f["rel"] in shared
    return {
        "kb_name": kb_name,
        "workspace": ws,
        "exists": exists,
        "root_name": root.name if root else "",
        "files": files,
    }


@router.get("/api/kb/{kb_name}/workspace/file")
def api_kb_workspace_file(kb_name: str, path: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    root = _workspace_base(conn, kb_name)
    if root is None:
        raise HTTPException(status_code=400, detail="该知识库未配置工作区")
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="工作区目录不存在")
    rel = (path or "").strip().lstrip("/\\")
    if not rel:
        raise HTTPException(status_code=400, detail="空路径")
    target = _safe_ws_path(root, rel)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}")
    return {"rel": rel, "content": content}


@router.get("/api/kb/{kb_name}/workspace/reveal")
def api_kb_workspace_reveal(
    kb_name: str, path: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """在资源管理器中打开工作区文件的所在目录（限于工作区根目录内，防路径穿越）。"""
    root = _workspace_base(conn, kb_name)
    if root is None or not root.is_dir():
        raise HTTPException(status_code=400, detail="该知识库未配置工作区")
    rel = (path or "").strip().lstrip("/\\")
    if not rel:
        raise HTTPException(status_code=400, detail="空路径")
    target = _safe_ws_path(root, rel)
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    folder = target.parent if target.is_file() else target
    if not folder.is_dir():
        raise HTTPException(status_code=400, detail="无法定位目录")
    try:
        os.startfile(str(folder))  # type: ignore[attr-defined]  # Windows 资源管理器
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"打开目录失败：{exc}")
    return {"ok": True, "folder": str(folder)}


class WorkspaceFileWrite(BaseModel):
    content: str = ""


@router.put("/api/kb/{kb_name}/workspace/file")
def api_kb_workspace_file_write(
    kb_name: str, path: str, payload: WorkspaceFileWrite, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    root = _workspace_base(conn, kb_name)
    if root is None:
        raise HTTPException(status_code=400, detail="该知识库未配置工作区")
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="工作区目录不存在")
    rel = (path or "").strip().lstrip("/\\")
    if not rel:
        raise HTTPException(status_code=400, detail="空路径")
    if Path(rel).suffix.lower() not in _WORKSPACE_EXTS:
        raise HTTPException(status_code=400, detail="仅允许写入可预览文本类型")
    target = _safe_ws_path(root, rel)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload.content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入失败：{exc}")
    workspace_sync.notify(kb_name)   # 工作区已改 → 后台防抖后自动提交到版本库
    return {"ok": True, "rel": rel, "size": target.stat().st_size}


@router.put("/api/kb/{kb_name}/workspace")
def api_kb_workspace_set(kb_name: str, payload: WorkspaceRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not kb.get_base(conn, kb_name):
        raise HTTPException(status_code=404, detail="知识库不存在")
    kb.set_workspace(conn, kb_name, payload.workspace)
    workspace_sync.notify(kb_name)   # 工作区目录可能变了 → 下轮重新建基线
    return {"ok": True, **api_kb_workspace(kb_name, conn)}


# ── 兼容旧接口：默认知识库（供 web_edit 发布按钮等仍可用）──────────


@router.get("/api/kb/categories")
def api_kb_categories_legacy(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"categories": kb.list_categories(conn, "")}


@router.post("/api/kb/categories")
def api_kb_category_add_legacy(payload: CategoryRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    p = kb.ensure_category(conn, "", payload.path)
    if not p:
        raise HTTPException(status_code=400, detail="空层级路径")
    return {"ok": True, "path": p, "categories": kb.list_categories(conn, "")}


@router.delete("/api/kb/categories/{path:path}")
def api_kb_category_del_legacy(path: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not kb.delete_category(conn, "", path):
        raise HTTPException(status_code=404, detail="层级不存在")
    return {"ok": True, "categories": kb.list_categories(conn, "")}


@router.post("/api/kb/categories/rename")
def api_kb_category_rename_legacy(payload: CategoryRename, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not kb.rename_category(conn, "", payload.old, payload.new):
        raise HTTPException(status_code=400, detail="重命名失败（路径为空或已相同）")
    return {"ok": True, "categories": kb.list_categories(conn, "")}


@router.get("/api/kb/articles")
def api_kb_articles_legacy(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return {"articles": kb.list_articles(conn, "")}


@router.post("/api/kb/publish")
def api_kb_publish_legacy(
    payload: PublishRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    _require_note_access_or_admin(request, conn, payload.note_path)
    note = file_store.get_note(notes_root, payload.note_path)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    owner = note_access.get_owner(conn, payload.note_path) or current_user(request)
    ws_rel = _copy_note_to_workspace(conn, "", payload.note_path, note.content, payload.category)
    kb.publish(
        conn,
        kb_name="",
        note_path=payload.note_path,
        title=note.title,
        content=note.content,
        category=payload.category,
        owner=owner,
        is_public=payload.is_public,
        workspace_rel=ws_rel,
    )
    if ws_rel:
        # 旧接口（无知识库名）归入默认知识库的映射
        _depot_upload_content(conn, kb._DEFAULT_BASE, ws_rel, note.content, payload.note_path)
    return {"ok": True, "article": kb.get_article(conn, "", payload.note_path)}


@router.post("/api/kb/unpublish")
def api_kb_unpublish_legacy(payload: PublishRequest, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    kb.unpublish(conn, "", payload.note_path)
    return {"ok": True}


@router.delete("/api/kb/{note_path:path}")
def api_kb_article_del_legacy(note_path: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    if not kb.unpublish(conn, "", note_path):
        raise HTTPException(status_code=404, detail="该笔记未发布到知识库")
    return {"ok": True}
