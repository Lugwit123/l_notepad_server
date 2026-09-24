# -*- coding: utf-8 -*-
"""知识库工作区 → depot 自动上传（后台线程，防抖）

为什么需要：知识库全文索引只从 depot 归档构建（见 search_index 模块说明），
本机工作区目录只用于编辑。用户在工作区保存文档后若不手动「提交到版本库」，
归档 rev 不变，索引也就一直不更新 —— 表现为「工作区改了 N 次，搜索还是旧内容」。
本模块把「工作区出现的改动」自动提交为 depot 新版本，从而带动 search_index 的
归档同步刷新索引。

防抖：周期轮询工作区（WS_SCAN_TTL_S，默认 5s），某文件 (size, mtime) 必须连续
WS_DEBOUNCE_S（默认 3s）不再变化后才可能提交，避免连续保存产生一串版本。
真正提交前还会拉归档最新内容做一次字节比对，和手工「提交」重复时不会多出版本。

不打扰：无 depot 登录态时整轮跳过；只处理可预览文本类型；单文件上限 4MB；
本地删除不联动删除归档（避免误删已归档版本）。

开关：L_NOTEPAD_WS_AUTOSYNC（默认 "1" 开；设 "0"/false 关闭）。
调参：L_NOTEPAD_WS_SCAN_TTL_S / L_NOTEPAD_WS_DEBOUNCE_S。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import paths
from . import search_index

# 轮询间隔（秒）
WS_SCAN_TTL_S = float(os.environ.get("L_NOTEPAD_WS_SCAN_TTL_S", "5") or 5)
# 防抖静默窗口（秒）：文件不变这么久才提交
WS_DEBOUNCE_S = float(os.environ.get("L_NOTEPAD_WS_DEBOUNCE_S", "3") or 3)
# 单文件自动上传上限（与托盘 kb_ws 写上限一致）
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
# 可自动上传的文本类型（与搜索索引 / 网页可预览白名单一致）
WORKSPACE_EXTS = search_index.WORKSPACE_EXTS

_lock = threading.Lock()
_wake = threading.Event()
_started = False
_last_run = 0.0

_kb_root: dict[str, str] = {}                              # kb -> 上次观察到的工作区根
_synced: dict[tuple[str, str], tuple[int, float]] = {}     # (kb, rel) -> 已同步 (size, mtime)
_pending: dict[tuple[str, str], tuple[int, float, float]] = {}  # (kb, rel) -> (size, mtime, 首见 mono)
_errors: dict[str, str] = {}


def enabled() -> bool:
    return os.environ.get("L_NOTEPAD_WS_AUTOSYNC", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ── 状态持久化（重启后不重复上传；同 size 改动也能靠 mtime 识别）──────


def _state_file() -> Path:
    return paths.data_root() / "ws_autosync.json"


def _load_state() -> None:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 无状态/损坏都按首次运行处理
        return
    kbs = (data or {}).get("kbs") or {}
    with _lock:
        for kb, info in kbs.items():
            info = info or {}
            _kb_root[str(kb)] = str(info.get("root") or "")
            for rel, val in (info.get("files") or {}).items():
                try:
                    _synced[(str(kb), str(rel))] = (int(val[0]), float(val[1]))
                except Exception:  # noqa: BLE001
                    continue


def _save_state() -> None:
    with _lock:
        out: dict[str, Any] = {}
        for (kb, rel), (size, mtime) in _synced.items():
            slot = out.setdefault(kb, {"root": _kb_root.get(kb, ""), "files": {}})
            slot["files"][rel] = [size, mtime]
    try:
        f = _state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "kbs": out}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, f)
    except Exception:  # noqa: BLE001 - 状态落盘失败不影响主流程
        pass


def _reset_kb(kb_name: str) -> None:
    with _lock:
        for k in [k for k in _synced if k[0] == kb_name]:
            _synced.pop(k, None)
        for k in [k for k in _pending if k[0] == kb_name]:
            _pending.pop(k, None)


# ── 单库扫描 ────────────────────────────────────────────


def _needs_upload(conn, kb_name: str, rel: str, content: bytes,
                  cloud: dict[str, int] | None) -> bool:
    """提交前的字节级确认：归档已有一致内容 → False（避免和手工提交重复）。"""
    from . import depot_map

    if cloud is not None and cloud.get(rel) == len(content):
        return False  # 首次建基线时的快速通道：同 size 视为已同步
    try:
        remote = depot_map.read_file(conn, kb_name, rel=rel, rev=0)
    except Exception:  # noqa: BLE001 - 归档没有/被删/取不到 → 视为需要上传
        return True
    return remote != content


def _scan_kb(conn, kb_name: str, root: Path, now: float) -> int:
    """扫描一个知识库工作区，提交静默已稳定的改动，返回上传数。"""
    from . import depot_map

    with _lock:
        known_root = _kb_root.get(kb_name)
    cloud: dict[str, int] | None = None
    if known_root != str(root):
        # 首次（或工作区根变化）→ 清状态并拉一次归档目录做同 size 快速基线
        _reset_kb(kb_name)
        try:
            files = depot_map.list_tree(conn, kb_name, exts=WORKSPACE_EXTS)["files"]
        except Exception as exc:  # noqa: BLE001 - depot 不可用只记状态
            with _lock:
                _errors[kb_name] = f"{type(exc).__name__}: {exc}"
            return 0
        cloud = {str(f["rel"]): int(f["size"]) for f in files}
        with _lock:
            _kb_root[kb_name] = str(root)
            _errors.pop(kb_name, None)

    uploaded = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in WORKSPACE_EXTS:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size > MAX_UPLOAD_BYTES:
            continue
        rel = p.relative_to(root).as_posix()
        key = (kb_name, rel)
        cur = (int(st.st_size), float(st.st_mtime))
        with _lock:
            if _synced.get(key) == cur:
                _pending.pop(key, None)
                continue
            pend = _pending.get(key)
            if pend is None or pend[0] != cur[0] or pend[1] != cur[1]:
                _pending[key] = (cur[0], cur[1], now)   # (重新)开始静默计时
                continue
            if now - pend[2] < WS_DEBOUNCE_S:
                continue  # 静默窗口未到
        try:
            content = p.read_bytes()
        except OSError:
            continue
        try:
            need = _needs_upload(conn, kb_name, rel, content, cloud)
        except Exception as exc:  # noqa: BLE001 - 比对失败先跳过，下轮再来
            with _lock:
                _errors[kb_name] = f"{type(exc).__name__}: {exc}"
                _pending.pop(key, None)
            continue
        if need:
            try:
                depot_map.submit_file(conn, kb_name, rel=rel, content=content,
                                      description="工作区自动上传 " + rel)
            except Exception as exc:  # noqa: BLE001 - 单文件失败不阻断整轮
                with _lock:
                    _errors[kb_name] = f"{type(exc).__name__}: {exc}"
                    _pending.pop(key, None)
                continue
            uploaded += 1
        with _lock:
            _synced[key] = cur
            _pending.pop(key, None)
            _errors.pop(kb_name, None)
    return uploaded


# ── 后台线程 ────────────────────────────────────────────


def _worker(db_path: Path) -> None:
    from . import db as dbmod
    from . import depot_map

    global _last_run
    _load_state()
    while True:
        _wake.wait(timeout=WS_SCAN_TTL_S)
        _wake.clear()
        if not enabled():
            continue
        try:
            if not depot_map.configured_login_state():
                continue  # 无登录态不打扰 depot，也不报错
            conn = dbmod.connect(db_path)
        except Exception:  # noqa: BLE001
            continue
        try:
            rows = conn.execute(
                "SELECT name, workspace FROM knowledge_bases WHERE TRIM(workspace) != ''"
            ).fetchall()
            for r in rows:
                kb_name = str(r["name"])
                root = Path(str(r["workspace"]).strip())
                if not root.is_dir():
                    continue
                try:
                    n = _scan_kb(conn, kb_name, root, time.monotonic())
                except Exception as exc:  # noqa: BLE001 - 单库失败不影响其它库
                    with _lock:
                        _errors[kb_name] = f"{type(exc).__name__}: {exc}"
                    continue
                if n:
                    # 归档已更新 → 让 search_index 后台即时重索引该库
                    search_index.notify_kb_change(kb_name)
            _save_state()
            with _lock:
                _last_run = time.monotonic()
        except Exception:  # noqa: BLE001 - 后台线程不因单轮失败退出
            pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def start(db_path: Path) -> bool:
    """启动工作区自动上传线程（幂等）。返回是否本次启动。"""
    global _started
    if not enabled():
        return False
    with _lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_worker, args=(Path(db_path),),
                     name="ws_autosync", daemon=True).start()
    return True


def notify(kb_name: str = "") -> None:
    """提示工作区可能已改动（如网页保存），立即唤醒一轮扫描（仍受防抖约束）。"""
    _wake.set()


def status() -> dict[str, Any]:
    """自动上传状态快照（状态页 / 排错用）。"""
    with _lock:
        return {
            "enabled": enabled(),
            "scan_ttl_s": WS_SCAN_TTL_S,
            "debounce_s": WS_DEBOUNCE_S,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "workspaces": len(_kb_root),
            "pending": len(_pending),
            "synced": len(_synced),
            "last_run_ago": round(time.monotonic() - _last_run, 1) if _last_run else None,
            "errors": dict(_errors),
        }


__all__ = ["enabled", "start", "notify", "status",
           "WS_SCAN_TTL_S", "WS_DEBOUNCE_S", "MAX_UPLOAD_BYTES"]
