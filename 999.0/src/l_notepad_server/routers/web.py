# -*- coding: utf-8 -*-
"""网页端：/ 登录后的页面路由 + 登录/登出 API + 服务器日志查看页。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from pytracemp import lprint

from .. import auth as authmod
from .. import depot_map
from .. import file_store
from .. import knowledge as kbmod
from .. import note_access
from .. import search_index
from .deps import (
    accessible_brief,
    current_user,
    get_conn,
    get_templates,
    is_admin,
    mounted_url,
    template_ctx,
    web_base,
)

router = APIRouter(tags=["web"])

_TAG_SPLIT = re.compile(r"[,，;；]+")

# 搜索模式说明（/web/search 帮助面板；键顺序即下拉顺序）
MODE_HELP: dict[str, str] = {
    "auto": "自动：先用词法搜（毫秒级），一条都没命中时才改用语义兜底。最省心，长句/自然语言推荐。",
    "lex": "仅词法：倒排索引精确匹配关键词，最快；适合关键词明确，长句可能搜不到。",
    "hybrid": "混合：词法为主 + 语义加分，召回更广；较慢（本机无 embedding 时可能数秒）。",
    "sem": "仅语义：按“意思相近”匹配，不看关键词是否出现；可能召回噪声，适合换词也找不到时。",
}


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


@dataclass(frozen=True)
class SearchHitView:
    """搜索结果条目视图：字段与 FileNote 对齐（模板复用），额外带来源与打开地址。"""

    path: str
    title: str
    content: str
    created_at: str
    updated_at: str
    url: str
    content_html: str = ""   # 命中词已 <mark> 的摘要（模板用 |safe）
    score: float = 0.0
    coverage: float = 0.0
    source: str = "note"
    kb_name: str = ""


def _hit_view(request: Request, hit: dict[str, Any], brief: list[file_store.FileNote]) -> SearchHitView:
    """搜索结果 → 模板条目：笔记指向编辑页，知识库工作区指向知识库页并定位文件。"""
    rel = str(hit.get("rel") or hit.get("path") or "")
    if hit.get("source") == "kb":
        url = f"{web_base(request)}/kb/{quote(str(hit.get('kb_name') or ''))}?file={quote(rel, safe='/')}"
        prefix = f"{hit.get('kb_name') or ''} / "
    else:
        url = f"{web_base(request)}/{quote(rel, safe='/')}"
        prefix = ""
    brief_map = {n.path: n for n in brief}
    base = brief_map.get(str(hit.get("path") or ""))
    snippet = str(hit.get("snippet") or "")
    return SearchHitView(
        path=str(hit.get("path") or rel),
        title=prefix + rel,
        content=snippet,
        content_html=search_index.highlight(snippet, list(hit.get("matches") or [])),
        score=float(hit.get("score") or 0.0),
        coverage=float(hit.get("coverage") or 0.0),
        created_at=base.created_at if base else str(hit.get("updated_at") or ""),
        updated_at=str(hit.get("updated_at") or ""),
        url=url,
        source=str(hit.get("source") or "note"),
        kb_name=str(hit.get("kb_name") or ""),
    )


def _hit_open_url(request: Request, hit: dict[str, Any]) -> str:
    """命中项的前端打开地址（与 routers/search.py 的 `_open_url` 保持一致）。"""
    rel = quote(str(hit.get("rel") or hit.get("path") or ""), safe="/")
    if hit.get("source") == "kb":
        return f"{web_base(request)}/kb/{quote(str(hit.get('kb_name') or ''))}?file={rel}"
    if hit.get("source") == "code":
        root = quote(str(hit.get("kb_name") or ""))
        return f"{mounted_url(request, 'api/search/code/file')}?root={root}&file={rel}"
    return f"{web_base(request)}/{rel}"


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/api/auth/login")
async def auth_login(payload: LoginRequest, response: Response) -> dict[str, Any]:
    try:
        data = await authmod.login(payload.username, payload.password)
    except authmod.AuthUnavailable:
        # 认证服务连不上/超时/证书校验失败：不能说成"密码错误"，否则用户只能反复试密码
        raise HTTPException(status_code=503, detail="认证服务不可用，请稍后重试或联系管理员")
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
    _maybe_seed_depot_login(payload, data)
    return {"access_token": data["access_token"], "token_type": "bearer", "user": data["user"]}


def _maybe_seed_depot_login(payload: LoginRequest, data: dict[str, Any]) -> None:
    """管理员网页登录且服务进程尚无 depot 登录态 → 自动落机器凭据文件。

    背景：depot KB 索引是**服务进程**的后台任务（无请求上下文），只认进程登录态；
    网页登录者的 token 在各自浏览器里，服务进程拿不到。这里把管理员本次登录的
    账号密码写成 `~/.lugwit/l_notepad_server/depot_auth.json`（0600，不入库不推送），
    之后索引/云同步即用该身份；账号密码改了 → 旧凭据登录失败 → 下次管理员登录自动重播。
    `LUGWIT_DEPOT_AUTO_SEED=0` 可关闭。
    """
    try:
        if (os.environ.get("LUGWIT_DEPOT_AUTO_SEED", "1").strip() == "0"):
            return
        user_info = data.get("user") or {}
        # 登录响应的 role 是角色名字符串（"admin"/"system"/"user"）；容错也收整数
        role = user_info.get("role")
        role_ok = role in (1, 2) or (
            isinstance(role, str) and role.strip().lower() in ("admin", "system"))
        if not role_ok:
            return
        if depot_map.configured_login_state():
            return
        path = depot_map.seed_login_state(payload.username, payload.password)
        lprint(f"[l_notepad][login] 已用管理员登录自动配置 depot 登录态 -> {path}")
    except Exception as exc:  # noqa: BLE001 —— 配置失败不阻断登录
        lprint(f"[l_notepad][login] depot 登录态自动配置失败: {exc!r}")


@router.post("/api/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie("l_notepad_token", path="/")
    return {"ok": True}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(request, "login.html", {**template_ctx(request)})


# ── 状态页 ──


@router.get("/")
def index(request: Request) -> RedirectResponse:
    """入口直接进「我的笔记」（原欢迎页 index.html 已删除）。"""
    return RedirectResponse(url=mounted_url(request, "web"), status_code=302)


# ── 服务器日志查看页（管理员）── 注意：必须在 /web/{note_path:path} 之前注册


@router.get("/web/logs", response_class=HTMLResponse)
def web_logs_page(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(request, "web_logs.html", {**template_ctx(request)})


# ── 搜索索引状态页 ── 注意：同样必须在 /web/{note_path:path} 之前注册


@router.get("/web/index", response_class=HTMLResponse)
def web_index_page(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(request, "web_index.html", {**template_ctx(request)})


# ── 全局搜索页（笔记 + 所有知识库）── 同样必须在 /web/{note_path:path} 之前注册


@router.get("/web/search", response_class=HTMLResponse)
def web_search(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    q: str = "",
    mode: str = "auto",
    sources: str = "",
    kb: str = "",
    rerank: str = "",
    limit: int = 100,
    offset: int = 0,
) -> HTMLResponse:
    """独立搜索页：一次搜「个人笔记 + 所有知识库归档」。

    参数：`q` 关键词；`mode` auto/lex/hybrid/sem；`sources` note/kb（逗号分隔）；
    `kb` 指定单个知识库（隐含 `sources=kb`）；`rerank` 0/1（受全局配置约束）；
    `limit`/`offset` 分页。检索一次取到上限（MAX_LIMIT=500）后在服务端切片，便于分面统计。
    """
    templates = get_templates(request)
    query = (q or "").strip()
    src = [s.strip() for s in (sources or "").split(",") if s.strip() in ("note", "kb", "code")]
    kb_name = (kb or "").strip()
    if kb_name:
        src = ["kb"]
    lim = max(1, min(int(limit or 100), 200))
    off = max(0, int(offset or 0))
    m = (mode or "auto").strip().lower()
    if m not in MODE_HELP:
        m = "auto"
    # rerank 用字符串接收：表单未选时提交空串，Optional[int] 会 int_parsing 报错
    rerank_raw = (rerank or "").strip()
    rr: bool | None = None
    if rerank_raw != "":
        try:
            rr = bool(int(rerank_raw))
        except ValueError:
            rr = None

    hits: list[SearchHitView] = []
    page_raw: list[dict[str, Any]] = []
    total = 0
    took_ms = 0.0
    fallback = False
    mode_used = ""
    vec_info: dict[str, Any] = {}
    kb_facets: list[dict[str, Any]] = []
    note_count = 0
    kb_count = 0
    shown_end = 0

    if query:
        common = dict(
            user=current_user(request),
            admin=is_admin(request),
            limit=search_index.MAX_LIMIT,
            offset=0,
            sources=src or None,
            rerank=rr,
            kb_name=kb_name,
        )
        if m == "auto":
            result = search_index.search_auto(conn, request.app.state.notes_root, query, **common)
            mode_used = result.get("mode_used", "")
        else:
            result = search_index.search(conn, request.app.state.notes_root, query, mode=m, **common)
            mode_used = m
        total = int(result.get("total") or 0)
        took_ms = float(result.get("took_ms") or 0.0)
        fallback = bool(result.get("fallback"))
        vec_info = dict(result.get("vec") or {})
        brief = accessible_brief(request, conn, limit=500)
        all_views = [_hit_view(request, h, brief) for h in result.get("hits") or []]
        counter: dict[str, int] = {}
        for h in all_views:
            if h.source == "kb":
                kb_count += 1
                if h.kb_name:
                    counter[h.kb_name] = counter.get(h.kb_name, 0) + 1
            else:
                note_count += 1
        kb_facets = [
            {"kb_name": k, "count": c}
            for k, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        hits = all_views[off : off + lim]
        shown_end = off + len(hits)
        # 原始命中（含全部打分明细）→ 前端复用 LN.renderSearchHits 渲染「丰富参数」卡片
        raw_hits = result.get("hits") or []
        page_raw = [
            {**h, "open_url": _hit_open_url(request, h)}
            for h in raw_hits[off : off + lim]
        ]

    try:
        kb_list = kbmod.list_bases(conn)
    except Exception:  # noqa: BLE001 知识库表缺失/异常时不阻断搜索页
        kb_list = []

    return templates.TemplateResponse(
        request,
        "web_search.html",
        {
            "q": q,
            "query": query,
            "mode": m,
            "mode_help": MODE_HELP,
            "mode_help_current": MODE_HELP.get(m, ""),
            "mode_used": mode_used,
            "sources": ",".join(src),
            "kb": kb_name,
            "kb_list": kb_list,
            "rerank": rerank_raw,
            "hits": hits,
            "hits_data": page_raw,
            "hits_json": json.dumps(page_raw, ensure_ascii=False).replace("<", "\\u003c"),
            "total": total,
            "took_ms": took_ms,
            "fallback": fallback,
            "vec_info": vec_info,
            "kb_facets": kb_facets,
            "note_count": note_count,
            "kb_count": kb_count,
            "limit": lim,
            "offset": off,
            "shown_start": off + 1 if hits else 0,
            "shown_end": shown_end,
            "has_prev": off > 0,
            "has_next": shown_end < total,
            "prev_offset": max(0, off - lim),
            "next_offset": off + lim,
            **template_ctx(request),
        },
    )


# ── 笔记列表 / 新建 / 编辑 ──


@router.get("/web", response_class=HTMLResponse)
def web_list(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    q: str = "",
    mode: str = "",
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
    query = (q or "").strip()
    fallback = False
    mode_used = ""
    if query:
        # 走 FTS5 倒排索引检索（毫秒级），不再逐文件读全文；命中行按相关度排序。
        # 同时含个人笔记与知识库归档命中，各自给出可打开的 url。
        # `mode=auto`（顶栏表单默认）：先词法，零命中再回退 hybrid，避免长句显示"无命中"。
        m = (mode or "hybrid").strip().lower()
        if m == "auto":
            result = search_index.search_auto(
                conn,
                request.app.state.notes_root,
                query,
                user=current_user(request),
                admin=is_admin(request),
                limit=500,
            )
            mode_used = result.get("mode_used", "")
        else:
            result = search_index.search(
                conn,
                request.app.state.notes_root,
                query,
                user=current_user(request),
                admin=is_admin(request),
                limit=500,
                mode=m,
            )
            mode_used = m
        notes = [_hit_view(request, h, notes) for h in result["hits"]]
        fallback = bool(result.get("fallback"))
    return templates.TemplateResponse(
        request,
        "web_list.html",
        {
            "notes": notes,
            "q": q,
            "mode": mode,
            "mode_used": mode_used,
            "fallback": fallback,
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
