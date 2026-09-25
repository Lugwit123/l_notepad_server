# -*- coding: utf-8 -*-
"""搜索 REST API：/api/search（FTS5 倒排索引检索）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import search_index
from .. import search_vec
from .deps import (
    current_user,
    get_conn,
    get_notes_root,
    is_admin,
    require_admin,
    web_base,
)

router = APIRouter(prefix="/api/search", tags=["search"])


def _open_url(request: Request, hit: dict[str, Any]) -> str:
    """命中项的前端打开地址：笔记 → 编辑页；知识库 → 知识库页；本机库 → 只读查看页。"""
    rel = quote(str(hit.get("rel") or hit.get("path") or ""), safe="/")
    if hit.get("source") == "kb":
        return f"{web_base(request)}/kb/{quote(str(hit.get('kb_name') or ''))}?file={rel}"
    if hit.get("source") == "code":
        label = quote(str(hit.get("kb_name") or ""))
        terms = [str(t) for t in list(hit.get("matches") or [])[:8]]
        url = f"{web_base(request)}/code?root={label}&file={rel}"
        return url + (f"&hl={quote(','.join(terms))}" if terms else "")
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
    packages: str = "",
) -> dict[str, Any]:
    """全文检索（倒排索引，毫秒级；权限过滤在 SQL 内完成）。

    - 宽召回：中文按 bigram OR 召回，命中短语 > 覆盖率 > bm25 排序；
      查询里用 `"引号"` 包住可要求精确短语。
    - `sources`：逗号分隔的索引源过滤（`note` 个人笔记 / `kb` 知识库归档 / `code` 本机库），默认全部。
    - `mode`：`hybrid`（默认，词法 + 语义加分，词法空时语义兜底）/ `lex`（纯词法）
      / `sem`（纯语义）/ `auto`（先 `lex`，命中太少或长句再回退 `hybrid`；返回多一个 `mode_used`）。
    - `packages`：逗号分隔的 **rez 源码包名**（见 `GET /api/search/code_packages`），
      只保留这些包的代码命中（`source=code`）；笔记与知识库不受影响。不传＝不限。
    - `rerank`：`0` 本次不用本地重排、`1` 使用（受全局配置约束），不传用全局配置。
    - 返回 hits：命中的来源、相对路径、打开地址 open_url、摘要、命中词 matches、
      块级信息 chunk / chunk_no / chunk_offset、覆盖率 / 词频 / 近邻 / bm25 / 语义相似度 vec /
      重排分 rerank / 总分 score，以及 rerank 汇总（是否使用 / 模型 / 条数 / 耗时 / 原因）。
    """
    src = [s.strip() for s in (sources or "").split(",") if s.strip()]
    pkgs = [s.strip() for s in (packages or "").split(",") if s.strip()]
    m = (mode or "hybrid").strip().lower()
    common = dict(
        user=current_user(request),
        admin=is_admin(request),
        limit=limit,
        offset=offset,
        sources=src or None,
        rerank=None if rerank is None else bool(rerank),
        packages=pkgs or None,
    )
    if m == "auto":
        result = search_index.search_auto(conn, notes_root, q, **common)
    else:
        result = search_index.search(conn, notes_root, q, mode=m, **common)
    hits = [{**h, "open_url": _open_url(request, h)} for h in result["hits"]]
    return {"query": q, "limit": limit, "offset": offset, **{**result, "hits": hits}}


@router.get("/route")
def api_route(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
    q: str = "",
    depth: int = 1,
    budget_ms: int = 300,
    limit: int = 10,
    sources: str = "kb,code",
) -> dict[str, Any]:
    """快速选库：复杂需求 → 相关库排序（供 Agent 决定读哪几个库）。

    - `depth`：`0` 仅库元数据匹配 / `1` 元数据 + 词法按库聚合（默认）/ `2` 语义加分
      （需求嵌一次与库内文档向量比对；语义不可用自动降级为 `1`）/ `3` 不处理
      （需求分解/多查询请调用方自行完成）。
    - `sources`：参与选库的来源，默认 `kb,code`（知识库归档 + 本机库：代码库根/知识库工作区）；
      只想要知识库传 `sources=kb`。
    - 与 `/api/search` 的区别：**不做段间 AND**（长需求不再零命中），且**只查索引表，
      不触发语义探测 / 网络**——毫秒级返回；慢路径一律降级。
    - `budget_ms`：软预算；`< 100` 时 `depth>=2` 自动退回 `1`（语义不做），并回填 `over_budget`。
    - 返回 `kbs` 按 `score` 降序，`degraded` / `reason` / `reason_code` 说明是否降档及原因；
      `terms` 为关键词块（含同义扩展），被 IDF 判为泛词剔除的见 `terms_generic_dropped`。
    """
    src = [s.strip() for s in (sources or "kb,code").split(",") if s.strip()]
    budget = int(budget_ms or 0)
    result = search_index.route(
        conn,
        notes_root,
        q,
        user=current_user(request),
        admin=is_admin(request),
        depth=depth,
        limit=limit,
        sources=src or None,
        budget_ms=budget,
    )
    result["budget_ms"] = budget
    result["over_budget"] = bool(budget) and result["took_ms"] > float(budget)
    return result


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


# ── 代码库索引（source=code）配置与查看 ────────────────────


class CodeRootsRequest(BaseModel):
    roots: list[str] = []


@router.get("/code_roots")
def api_code_roots(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """当前配置的代码库索引根目录（source=code）。"""
    return {
        "roots": [
            {"label": lb, "root": str(rp), "exists": rp.is_dir()}
            for lb, rp in search_index.code_roots(conn)
        ],
        "exts": sorted(search_index.CODE_EXTS),
    }


@router.put("/code_roots")
def api_set_code_roots(
    payload: CodeRootsRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    """设置代码库索引根目录（管理员），随后触发一次后台重建纳入索引。

    传空列表即清空（重建后 source=code 的索引行被移除）。
    """
    require_admin(request)
    search_index.set_code_roots(conn, payload.roots)
    started = search_index.start_reindex(request.app.state.db_path, notes_root)
    return {
        "ok": True,
        "reindex_started": started,
        "roots": [
            {"label": lb, "root": str(rp), "exists": rp.is_dir()}
            for lb, rp in search_index.code_roots(conn)
        ],
    }


@router.get("/code_packages")
def api_code_packages(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """可勾选搜索的本机库（搜索页「要搜索哪些包」的数据源）。

    含三类：`code` 代码库根（`code_roots` 配的目录，如 `l_notepad_client`）/ `pkg` rez 源码包
    （货架 `L_NOTEPAD_PKG_ROOT` 下带 `package.py` 的一级目录）/ `kbws` 知识库工作区。
    `docs` > 0 表示已建过索引（可被搜到）；没建过的要先在搜索页「索引管理」里建
    （或 `POST /api/search/index_lib`）。
    """
    rows = search_index.lib_rows(conn)
    rows.sort(key=lambda x: (0 if x["kind"] == "code" else 1, x["label"].lower()))
    root = search_index.pkg_root(conn)
    return {
        "root": str(root) if root else "",
        "packages": [
            {
                "label": x["label"],
                "kind": x["kind"],
                "root": x["root"],
                "exists": x["exists"],
                "docs": x["docs"],
                "indexed": x["docs"] > 0,
                "scan": x["scan"],
            }
            for x in rows
        ],
        "exts": sorted(search_index.CODE_EXTS),
        "max_files": search_index.CODE_MAX_FILES,
        "max_bytes": search_index.CODE_MAX_BYTES,
    }


@router.get("/index_libs")
def api_index_libs(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    """可手动建索引的本机库（代码库根 + 知识库工作区）与最近一次扫描状态。

    `kind`：`code` 代码库根（TTL 自动刷新）/ `kbws` 知识库工作区（只在手动建索引时扫）。
    """
    state = {str(s["label"]): s for s in search_index.code_lib_state()}
    docs = {
        str(r["kb_name"]): int(r["docs"])
        for r in conn.execute(
            "SELECT kb_name, COUNT(*) AS docs FROM search_docs WHERE source = 'code' GROUP BY kb_name"
        )
    }
    libs: list[dict[str, Any]] = []
    for lib in search_index.local_libs(conn):
        label = str(lib["label"])
        libs.append({
            "label": label,
            "kind": lib["kind"],
            "name": lib["name"],
            "root": str(lib["root"]),
            "exists": bool(lib["exists"]),
            "docs": docs.get(label, 0),
            "scan": state.get(label) or {},
        })
    return {
        "libs": libs,
        "exts": sorted(search_index.CODE_EXTS),
        "max_files": search_index.CODE_MAX_FILES,
        "max_bytes": search_index.CODE_MAX_BYTES,
    }


class IndexLibRequest(BaseModel):
    label: str = ""
    embed: bool = True


@router.post("/index_lib")
def api_index_lib(
    payload: IndexLibRequest,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    notes_root: Path = Depends(get_notes_root),
) -> dict[str, Any]:
    """手动为一个本机库建索引（管理员）：搜索页「创建索引」调用。

    只扫该库目录（`CODE_EXTS` 过滤 + 体量上限），**不触发 depot 上传**；`embed=True`
    时随后台向量嵌入线程把这批代码也嵌入（语义检索可用）。
    """
    require_admin(request)
    label = (payload.label or "").strip()
    if not label:
        return {"ok": False, "error": "label 不能为空"}
    try:
        result = search_index.index_local_lib(conn, label)
    except KeyError:
        return {"ok": False, "error": f"未知的本机库：{label}"}
    embed_started = False
    if payload.embed:
        try:
            embed_started = search_vec.start_embed_async(request.app.state.db_path, notes_root)
        except Exception:  # noqa: BLE001 - 嵌入失败不影响词法索引
            embed_started = False
    return {"ok": True, "embed_started": embed_started, **result}


@router.get("/code/file")
def api_code_file(
    conn: sqlite3.Connection = Depends(get_conn),
    root: str = "",
    file: str = "",
) -> PlainTextResponse:
    """只读查看本机库文件（`source=code` 命中项的打开地址）；路径限定在库根内。

    `root` 为库标签（代码库根或知识库工作区，见 `GET /api/search/index_libs`）。
    """
    roots = search_index.local_lib_map(conn)
    base = roots.get(root)
    if base is None:
        return PlainTextResponse("未知代码库: " + root, status_code=404)
    try:
        target = (base / file).resolve()
        target.relative_to(base.resolve())
    except (ValueError, OSError):
        return PlainTextResponse("非法路径", status_code=400)
    if not target.is_file():
        return PlainTextResponse("文件不存在", status_code=404)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return PlainTextResponse("读取失败: " + str(exc), status_code=500)
    return PlainTextResponse(text)
