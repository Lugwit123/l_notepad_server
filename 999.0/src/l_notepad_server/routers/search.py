# -*- coding: utf-8 -*-
"""搜索 REST API：/api/search（FTS5 倒排索引检索）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from .. import search_index
from .. import search_vec
from .deps import current_user, get_conn, get_notes_root, is_admin, require_admin, web_base

router = APIRouter(prefix="/api/search", tags=["search"])


def _open_url(request: Request, hit: dict[str, Any]) -> str:
    """命中项的前端打开地址：笔记 → 编辑页；知识库归档 → 知识库页并定位文档。"""
    rel = quote(str(hit.get("rel") or hit.get("path") or ""), safe="/")
    if hit.get("source") == "kb":
        return f"{web_base(request)}/kb/{quote(str(hit.get('kb_name') or ''))}?file={rel}"
    return f"{web_base(request)}/{rel}"


@router.get("")
def api_search(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    sources: str = "",
    mode: str = "hybrid",
    rerank: Optional[int] = None,
) -> dict[str, Any]:
    """全文检索（倒排索引，毫秒级；权限过滤在 SQL 内完成）。

    - 宽召回：中文按 bigram OR 召回，命中短语 > 覆盖率 > bm25 排序；
      查询里用 `"引号"` 包住可要求精确短语。
    - `sources`：逗号分隔的索引源过滤（`note` 个人笔记 / `kb` 知识库归档），默认全部。
    - `mode`：`hybrid`（默认，词法 + 语义加分，词法空时语义兜底）/ `lex`（纯词法）/ `sem`（纯语义）。
    - `rerank`：`0` 本次不用本地重排、`1` 使用（受全局配置约束），不传用全局配置。
    - 返回 hits：命中的来源、相对路径、打开地址 open_url、摘要、命中词 matches、
      块级信息 chunk / chunk_no / chunk_offset、覆盖率 / 词频 / 近邻 / bm25 / 语义相似度 vec /
      重排分 rerank / 总分 score，以及 rerank 汇总（是否使用 / 模型 / 条数 / 耗时 / 原因）。
    """
    src = [s.strip() for s in (sources or "").split(",") if s.strip()]
    result = search_index.search(
        conn,
        notes_root,
        q,
        user=current_user(request),
        admin=is_admin(request),
        limit=limit,
        offset=offset,
        sources=src or None,
        mode=(mode or "hybrid").strip().lower(),
        rerank=None if rerank is None else bool(rerank),
    )
    hits = [{**h, "open_url": _open_url(request, h)} for h in result["hits"]]
    return {"query": q, "limit": limit, "offset": offset, **{**result, "hits": hits}}


@router.post("/embed_async")
def api_embed_async(
    request: Request,
    notes_root: Path = Depends(get_notes_root),
    force: int = 0,
) -> dict[str, Any]:
    """后台增量嵌入（管理员）：把新增/变更文档送去本机 embedding，进度见 stats。"""
    require_admin(request)
    started = search_vec.start_embed_async(request.app.state.db_path, notes_root, force=bool(force))
    return {
        "ok": True,
        "started": started,
        "message": "已开始嵌入" if started else "嵌入已在运行中",
        "vec": dict(search_vec.embed_state),
        "available": search_vec.available(),
    }


class ModelRequest(BaseModel):
    model: str = ""


@router.get("/models")
def api_models(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """可选 embedding 模型清单（是否已安装）+ 当前模型 + 下载进度。"""
    return search_vec.model_status(conn)


@router.post("/model")
def api_set_model(
    request: Request,
    payload: ModelRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """切换 embedding 模型（管理员）。

    未安装时不下载，只回 `need_download=True`，由前端询问用户是否下载。
    """
    require_admin(request)
    return search_vec.set_model(conn, payload.model)


@router.post("/model/download")
def api_download_model(
    request: Request,
    payload: ModelRequest,
) -> dict[str, Any]:
    """下载 embedding 模型（管理员，需用户显式确认后才调用）。"""
    require_admin(request)
    name = (payload.model or "").strip()
    if not name:
        return {"ok": False, "error": "模型名不能为空"}
    started = search_vec.start_download(name)
    return {
        "ok": started,
        "started": started,
        "model": name,
        "message": f"已开始下载 {name}" if started else "已有下载在进行",
        "download": dict(search_vec.download_state),
    }


class RerankRequest(BaseModel):
    enabled: Optional[bool] = None
    model: Optional[str] = None


@router.get("/rerank")
def api_rerank_status(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """本地重排状态（可用性 / 模型 / 候选上限 / 超时 / 最近耗时 / 降级原因）。"""
    return search_vec.rerank_status(conn)


@router.post("/rerank")
def api_set_rerank(
    request: Request,
    payload: RerankRequest,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """切换本地重排开关 / 模型（管理员）。"""
    require_admin(request)
    return search_vec.set_rerank(conn, enabled=payload.enabled, model=payload.model)


@router.get("/stats")
def api_stats(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
    deep: int = 0,
) -> dict[str, Any]:
    """索引状态：文档数 / 分来源明细 / 增量队列 / 预热与后台重建进度。

    `deep=1` 额外校对（笔记比对磁盘、知识库比对归档 rev）与 FTS 完整性检查，较慢。
    """
    return search_index.stats(conn, notes_root, deep=bool(deep))


@router.get("/history")
def api_history(
    conn: sqlite3.Connection = Depends(get_conn),
    limit: int = 50,
) -> dict[str, Any]:
    """索引重建/同步历史（倒序）：时间 / 类型 / 目标 / 触发源 / 变更数 / 耗时。

    类型：`rebuild`（全量重建）/ `kb_sync`（知识库同步）/ `warm`（启动预热）；
    触发源：`manual` / `manual_async` / `event` / `ticker` / `startup`。
    """
    return {"history": search_index.history(conn, limit=limit)}


@router.post("/reindex")
def api_reindex(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    """同步重建全部索引（管理员）：例如迁移数据或改了归档映射后手动刷新。"""
    require_admin(request)
    return {"ok": True, "indexed": search_index.reindex(conn, notes_root)}


@router.post("/reindex_async")
def api_reindex_async(
    request: Request,
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    """后台重建全部索引（管理员）：立即返回，进度见 `GET /api/search/stats`。"""
    require_admin(request)
    started = search_index.start_reindex(request.app.state.db_path, notes_root)
    return {
        "ok": True,
        "started": started,
        "message": "已开始重建" if started else "重建已在运行中",
        "reindex": search_index.reindex_state(),
    }
