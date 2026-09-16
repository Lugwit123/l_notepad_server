# -*- coding: utf-8 -*-
"""知识库 ↔ 百度云版本库（depot）归档映射

设计：**库 = 逻辑路径首段**（默认 `/notes`，即 l_notepad 笔记库，dir 模式），
知识库只是该库下的一个**子路径**：
    depot 逻辑路径 = {library}/{subpath}/{rel}      如 /notes/rez_pkg/xxx.md

每个知识库对应一个 depot 工作区（P4 client 语义）：
    name     = kb-<知识库名>（可被 knowledge_bases.depot_ws 覆盖）
    library  = /notes（可被 depot_library 覆盖）
    local_root = 知识库工作区本地目录（knowledge_bases.workspace，可空）
    maps     = [{depot_path: {base_path}, local_path: ""}]     ← 子路径 ↔ 本地目录根
映射登记走 `PUT /api/depot/workspace/{id}/maps`，使本地 `xxx.md`
↔ `/notes/<kb>/xxx.md` 一一对应。

本模块**不依赖 fastapi**（可 headless 测试）；失败统一抛 `DepotError`，
由路由层转成 HTTP 错误。
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import knowledge as kbmod

DEFAULT_LIBRARY = "/notes"
DEFAULT_WS_PREFIX = "kb-"


class DepotError(RuntimeError):
    """版本库服务/映射相关错误。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def base_url() -> str:
    return os.environ.get("L_DEPOT_SERVICE_URL", "http://127.0.0.1:1028").strip().rstrip("/")


def http(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: bytes | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes]:
    """调用 depot HTTP 接口，返回 (status, raw)；网络异常抛 DepotError。"""
    url = base_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = body
    headers: dict[str, str] = {}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif body is not None:
        headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        raise DepotError(f"版本库服务不可用：{exc}") from exc


def _json(raw: bytes) -> dict[str, Any]:
    try:
        return json.loads(raw.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return {}


def mapping(conn: sqlite3.Connection, kb_name: str) -> dict[str, Any]:
    """解析知识库的归档映射（不访问 depot 服务）。"""
    base = kbmod.get_base(conn, kb_name)
    if not base:
        raise DepotError(f"知识库不存在：{kb_name}")
    library = str(base["depot_library"] or DEFAULT_LIBRARY).strip().rstrip("/")
    if not library.startswith("/"):
        library = "/" + library
    if library in ("", "/"):
        library = DEFAULT_LIBRARY
    subpath = str(base["depot_subpath"] or "").strip().strip("/") or kb_name
    ws_name = str(base["depot_ws"] or "").strip() or f"{DEFAULT_WS_PREFIX}{kb_name}"
    return {
        "kb_name": kb_name,
        "title": base["title"],
        "library": library,
        "subpath": subpath,
        "base_path": f"{library}/{subpath}",
        "ws_name": ws_name,
        "local_root": str(base["workspace"] or "").strip(),
        "remote_url": f"{base_url()}/depot?path={urllib.parse.quote(f'{library}/{subpath}', safe='')}",
    }


def depot_path(info: dict[str, Any], rel: str) -> str:
    return f"{info['base_path']}/{str(rel or '').lstrip('/')}"


def ensure_workspace(conn: sqlite3.Connection, kb_name: str) -> dict[str, Any]:
    """确保知识库专属 depot 工作区存在并登记子路径映射，返回带 ws_id 的映射信息。"""
    info = mapping(conn, kb_name)
    status, raw = http("GET", "/api/depot/workspace")
    data = _json(raw)
    existing = next(
        (w for w in data.get("workspaces", []) if w.get("name") == info["ws_name"]), None
    )
    payload: dict[str, Any] = {
        "name": info["ws_name"],
        "library": info["library"],
        "local_root": info["local_root"],
    }
    if existing:
        payload["id"] = existing["id"]
    status, raw = http("POST", "/api/depot/workspace", json_body=payload)
    if status >= 400:
        raise DepotError(f"创建版本库工作区失败：HTTP {status} {raw[:200]!r}")
    ws = _json(raw)
    ws = ws.get("workspace") or ws
    ws_id = int(ws.get("id") or (existing or {}).get("id") or 0)
    info["ws_id"] = ws_id
    info["maps"] = [{"depot_path": info["base_path"], "local_path": ""}]
    if ws_id:
        status, raw = http(
            "PUT", f"/api/depot/workspace/{ws_id}/maps", json_body={"maps": info["maps"]}
        )
        if status >= 400:
            raise DepotError(f"登记工作区映射失败：HTTP {status} {raw[:200]!r}")
        if info["ws_name"] != str(kbmod.get_base(conn, kb_name)["depot_ws"] or ""):
            conn.execute(
                "UPDATE knowledge_bases SET depot_ws = ?, updated_at = ? WHERE name = ?",
                (info["ws_name"], _now(), kb_name),
            )
            conn.commit()
    return info


def set_mapping(conn: sqlite3.Connection, kb_name: str, *, library: str | None = None,
                subpath: str | None = None, ws_name: str | None = None) -> dict[str, Any]:
    """修改知识库归档映射（库 / 子路径 / 工作区名），随后重建工作区映射。"""
    if not kbmod.get_base(conn, kb_name):
        raise DepotError(f"知识库不存在：{kb_name}")
    sets: list[str] = []
    args: list[Any] = []
    if library is not None:
        lib = library.strip().rstrip("/") or DEFAULT_LIBRARY
        if not lib.startswith("/"):
            lib = "/" + lib
        sets.append("depot_library = ?")
        args.append(lib)
    if subpath is not None:
        sets.append("depot_subpath = ?")
        args.append(subpath.strip().strip("/"))
    if ws_name is not None:
        sets.append("depot_ws = ?")
        args.append(ws_name.strip())
    if sets:
        sets.append("updated_at = ?")
        args.extend([_now(), kb_name])
        conn.execute(f"UPDATE knowledge_bases SET {', '.join(sets)} WHERE name = ?", args)
        conn.commit()
    return ensure_workspace(conn, kb_name)


def upload_content(conn: sqlite3.Connection, kb_name: str, rel: str, content: str,
                   note_path: str = "") -> bool:
    """把内容提交到 `{library}/{subpath}/{rel}`；失败返回 False（仅记录日志）。"""
    info = ensure_workspace(conn, kb_name)
    dpath = depot_path(info, rel)
    params: dict[str, Any] = {
        "path": dpath,
        "description": "来自个人笔记分享" + (f": {note_path}" if note_path else ""),
    }
    if info.get("ws_id"):
        params["ws"] = str(info["ws_id"])
    status, raw = http("POST", "/api/depot/submit_stream", params=params,
                       body=content.encode("utf-8"))
    if status >= 400:
        raise DepotError(f"提交失败：HTTP {status} {raw[:200]!r}")
    return True


def list_dir(conn: sqlite3.Connection, kb_name: str, *, rel: str = "") -> dict[str, Any]:
    """列出知识库归档子路径下的内容（默认根）。"""
    info = ensure_workspace(conn, kb_name)
    dpath = info["base_path"] if not rel.strip() else depot_path(info, rel)
    params = {"dir": dpath}
    if info.get("ws_id"):
        params["ws"] = str(info["ws_id"])
    status, raw = http("GET", "/api/depot/list", params=params)
    if status >= 400:
        raise DepotError(f"列目录失败：HTTP {status} {raw[:200]!r}")
    data = _json(raw)
    prefix = info["base_path"]
    items = []
    for it in data.get("items", []) or []:
        path = str(it.get("path") or "")
        rel_path = path[len(prefix):].lstrip("/") if path.startswith(prefix) else path.lstrip("/")
        items.append({**it, "rel": rel_path})
    return {"mapping": info, "dir": dpath, "items": items}


def read_file(conn: sqlite3.Connection, kb_name: str, *, rel: str, rev: int = 0) -> bytes:
    """读取知识库归档文件内容（rev=0 最新）。"""
    info = ensure_workspace(conn, kb_name)
    params = {"path": depot_path(info, rel), "rev": int(rev), "inline": 1}
    if info.get("ws_id"):
        params["ws"] = str(info["ws_id"])
    status, raw = http("GET", "/api/depot/download", params=params)
    if status >= 400:
        raise DepotError(f"读取失败：HTTP {status} {raw[:200]!r}")
    return raw


def submit_file(conn: sqlite3.Connection, kb_name: str, *, rel: str, content: bytes,
                description: str = "") -> dict[str, Any]:
    """把本地内容提交为知识库归档文件的一个新版本。"""
    info = ensure_workspace(conn, kb_name)
    params = {
        "path": depot_path(info, rel),
        "description": description or f"提交 {rel}",
    }
    if info.get("ws_id"):
        params["ws"] = str(info["ws_id"])
    status, raw = http("POST", "/api/depot/submit_stream", params=params, body=content)
    if status >= 400:
        raise DepotError(f"提交失败：HTTP {status} {raw[:200]!r}")
    return {"mapping": info, "path": depot_path(info, rel), "result": _json(raw)}
