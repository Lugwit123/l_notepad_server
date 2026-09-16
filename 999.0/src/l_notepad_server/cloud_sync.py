# -*- coding: utf-8 -*-
"""l_notepad 笔记本地 ↔ 百度网盘 双向镜像同步（进程内后台线程）。

策略（用户确认：本地+云端双向镜像）：
  - 本地 notes_dir 仍是工作区，笔记增删改通过 file_store 变更钩子触发推送（防抖队列）。
  - 后台定时轮询远端，若远端自 server_mtime 起更新或本地缺失则下载到本地，
    并把新出现的笔记注册（note_access.register_note）给 sync_owner，使其出现在列表。
  - 百度凭据/token 由 lugwit_baidu_netdisk 的 Web 服务统一持有与管理；
    l_notepad 不再 import 该库，而是通过其 HTTP 接口（同机 127.0.0.1:1028）
    完成 递归列举 / 上传 / 删除 / 下载。服务未运行或鉴权失败时优雅降级（跳过）。

配置：<pathlib.paths.data_root()>/cloud_sync.yaml
  也可用环境变量 L_CLOUD_SYNC_ENABLED / L_CLOUD_SYNC_FOLDER / L_CLOUD_SYNC_SUBPATH /
  L_CLOUD_SYNC_OWNER / L_CLOUD_SYNC_POLL 覆盖。
  网盘 Web 服务地址：L_CLOUD_SYNC_SERVICE_URL（默认 http://127.0.0.1:1028）。

注：本模块是纯 stdlib（urllib），不依赖 lugwit_baidu_netdisk / PyYAML（yaml 仅配置可选）。
"""

from __future__ import annotations

import atexit
import copy
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

try:
    from pytracemp import lprint
except Exception:  # pragma: no cover - 极简环境兜底
    def lprint(*args: Any, **kwargs: Any) -> None:
        import builtins

        builtins.print(*args, **kwargs)


from . import db as dbmod
from . import file_store, note_access, paths

_DEFAULTS: dict[str, Any] = {
    "cloud_sync": {
        "enabled": False,
        "app_folder": "",
        "remote_subpath": "notes",
        "sync_owner": "admin01",
        "register_pulled": True,
        "push_on_change": True,
        "pull_on_start": True,
        "poll_interval_seconds": 120.0,
        "sync_service_url": "http://127.0.0.1:1028",
        "files_base": "",
        "files_prefix": "/baidu",
        # 新百度云服务（depot 版本库）：笔记作为 dir 模式的库 /notes，
        # 物理落在 version_depot/dir_mirror/notes，推送走 /api/depot/submit_stream
        # （每次保存 = 一个版本，可在 depot 页面看历史/差异）。
        "use_depot": True,
        "depot_library": "/notes",
        "depot_workspace": "notes-sync",
    },
}

_QUEUE_ITEM = tuple[str, str]  # (action, rel_path)

_cfg_cache: Optional[dict[str, Any]] = None
_cfg_mtime = 0.0

_notes_root: Optional[Path] = None
_db_path: Optional[Path] = None

_enabled = False
_q: "queue.Queue[_QUEUE_ITEM]" = queue.Queue()
_pull_stop = threading.Event()
_pull_lock = threading.Lock()
_threads: list[threading.Thread] = []
_last_pull_at = 0.0
_last_error = ""


# ── 配置 ─────────────────────────────────────────

def _config_file() -> Path:
    return paths.data_root() / "cloud_sync.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _apply_env(cfg: dict) -> None:
    cs = cfg["cloud_sync"]
    raw_env = {
        "enabled": "L_CLOUD_SYNC_ENABLED",
        "app_folder": "L_CLOUD_SYNC_FOLDER",
        "remote_subpath": "L_CLOUD_SYNC_SUBPATH",
        "sync_owner": "L_CLOUD_SYNC_OWNER",
        "poll_interval_seconds": "L_CLOUD_SYNC_POLL",
        "sync_service_url": "L_CLOUD_SYNC_SERVICE_URL",
        "files_base": "L_CLOUD_SYNC_FILES_BASE",
        "files_prefix": "L_CLOUD_SYNC_FILES_PREFIX",
        "use_depot": "L_CLOUD_SYNC_USE_DEPOT",
        "depot_library": "L_CLOUD_SYNC_DEPOT_LIB",
        "depot_workspace": "L_CLOUD_SYNC_DEPOT_WS",
    }
    for key, env in raw_env.items():
        val = os.environ.get(env, "")
        if not val:
            continue
        if key in ("enabled", "use_depot"):
            cs[key] = val.strip().lower() in ("1", "true", "yes", "on")
        elif key == "poll_interval_seconds":
            try:
                cs[key] = float(val)
            except ValueError:
                pass
        else:
            cs[key] = str(val).strip()


def _load_cfg() -> dict[str, Any]:
    global _cfg_cache, _cfg_mtime
    p = _config_file()
    try:
        m = p.stat().st_mtime if p.exists() else 0.0
    except OSError:
        m = 0.0
    if _cfg_cache is not None and abs(m - _cfg_mtime) <= 1e-6:
        return _cfg_cache
    cfg = copy.deepcopy(_DEFAULTS)
    if p.exists():
        try:
            import yaml  # type: ignore[import-untyped]

            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                cfg = _deep_merge(cfg, raw)
        except Exception:
            pass
    _apply_env(cfg)
    _cfg_cache = cfg
    _cfg_mtime = m
    return cfg


def _cfg_get(key: str):
    return _load_cfg()["cloud_sync"].get(key)


# ── 网盘 Web 服务 HTTP ────────────────────────

def _base_url() -> str:
    """网盘 Web 服务地址（同机 127.0.0.1:1028，独立于 nginx 统一入口）。"""
    return str(_cfg_get("sync_service_url") or "http://127.0.0.1:1028").strip().rstrip("/")


def _service_ready() -> bool:
    """网盘 Web 服务是否在线（服务 + 本地自动鉴权通过）。"""
    try:
        data = _http("GET", "/api/state")
        return isinstance(data, dict)
    except Exception:
        return False


def _http(method: str, path: str, json_body: Any = None, timeout: int = 120) -> Any:
    """向网盘 Web 服务发起请求：返回解析后的 JSON；非 2xx 抛 RuntimeError。

    鉴权：服务端对 127.0.0.1 本地客户端自动授权（_current_user），
    l_notepad 与网盘服务同机，故无须自带凭据，服务端复用其持有的百度 token。
    """
    url = _base_url() + path
    body = None
    headers = {"Accept": "application/json"}
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"[cloud_sync] HTTP {method} {path} -> {exc.code}: {detail}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def _remote_apps_root() -> str:
    """从网盘 Web 服务拉取 apps 应用根目录（/apps/<folder>）。"""
    data = _http("GET", "/api/sync/apps_root")
    if not isinstance(data, dict) or not data.get("apps_root"):
        raise RuntimeError("无法获取网盘 apps_root（服务未就绪或未授权）")
    return str(data["apps_root"])


# ── 启用判断 ─────────────────────────────────────────

def enabled() -> bool:
    if not bool(_cfg_get("enabled")):
        return False
    if not _service_ready():
        lprint("[cloud_sync] 网盘 Web 服务未在线，云端镜像未启动")
        return False
    return True


# ── 路径映射 ─────────────────────────────────────────

def _use_depot() -> bool:
    return bool(_cfg_get("use_depot"))


def _depot_library() -> str:
    """笔记库 root（dir 模式），默认 /notes。"""
    lib = str(_cfg_get("depot_library") or "").strip()
    if not lib:
        sub = str(_cfg_get("remote_subpath") or "notes").strip("/") or "notes"
        lib = "/" + sub
    return "/" + lib.strip("/")


def _depot_path(rel: str) -> str:
    return f"{_depot_library()}/{rel.replace('\\', '/').strip('/')}"


def _remote_base() -> str:
    """远端基路径。

    新服务（use_depot，默认）：depot 库的 dir 镜像 =
        {apps}/version_depot/dir_mirror/<库>     ← 活文件就是最新版
    旧约定：{apps}[/<app_folder>]/<remote_subpath>（裸目录）
    app_folder 配置时用 /apps/<folder>。
    """
    folder = str(_cfg_get("app_folder") or "").strip()
    if folder:
        apps = "/apps/" + folder.strip("/")
    else:
        apps = _remote_apps_root()
    sub = str(_cfg_get("remote_subpath") or "").strip("/")
    if _use_depot():
        lib = _depot_library().strip("/")
        return f"{apps}/version_depot/dir_mirror/{lib}"
    return (apps + "/" + sub).replace("//", "/") if sub else apps


_ws_id: Optional[int] = None


def _depot_ws() -> int:
    """笔记库的工作区 id（提交必须带 ws）：没有就建一个，缓存住。"""
    global _ws_id
    if _ws_id:
        return _ws_id
    lib = _depot_library()
    want = str(_cfg_get("depot_workspace") or "notes-sync").strip() or "notes-sync"
    data = _http("GET", "/api/depot/workspace")
    for w in (data or {}).get("workspaces") or []:
        if str(w.get("library")) == lib and str(w.get("name")) == want:
            _ws_id = int(w.get("id") or 0)
            return _ws_id
    res = _http("POST", "/api/depot/workspace",
                {"name": want, "library": lib, "local_root": "", "host": ""})
    _ws_id = int(((res or {}).get("workspace") or {}).get("id") or 0)
    if not _ws_id:
        raise RuntimeError(f"创建笔记工作区失败: {res}")
    lprint(f"[cloud_sync] 已创建工作区 {want} (ws={_ws_id}) 库={lib}")
    return _ws_id


def _local_path(rel: str) -> Path:
    seg = rel.replace("\\", "/").strip("/").split("/")
    if not seg or ".." in seg:
        raise ValueError(f"非法相对路径: {rel!r}")
    return _notes_root.joinpath(*seg)  # type: ignore[union-attr]


def _rel_from_base(remote_base: str, fp: str) -> Optional[str]:
    rb = remote_base.rstrip("/")
    f = str(fp).replace("\\", "/").rstrip("/")
    if f == rb:
        return None
    prefix = rb + "/"
    if not f.startswith(prefix):
        return None
    return f[len(prefix):]


def _remote_path(rel: str) -> str:
    return f"{_remote_base()}/{rel.replace('\\', '/')}".replace("//", "/")


# ── 推送（本地 → 云端） ─────────────────────────────────────────

def _http_raw(path: str, data: bytes, timeout: int = 300) -> Any:
    """POST 原始字节（/api/depot/submit_stream 这类流式上传接口用）。"""
    req = urllib.request.Request(
        _base_url() + path, data=data, method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"[cloud_sync] HTTP POST {path} -> {exc.code}: {detail}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def _push_upsert(rel: str) -> None:
    local = _local_path(rel)
    if _use_depot():
        # 一步提交：服务端写 blob + dir 镜像活文件 + 一个版本；每次保存 = 一版
        dpath = _depot_path(rel)
        qs = urllib.parse.urlencode({
            "path": dpath,
            "description": f"笔记 {rel}",
            "ws": _depot_ws(),
        })
        _http_raw(
            f"/api/depot/submit_stream?{qs}",
            local.read_bytes(),
        )
        return
    remote = _remote_path(rel)
    remote_dir = str(Path(remote).parent).replace("\\", "/")
    remote_name = str(Path(remote).name)
    _http(
        "POST",
        "/api/files/upload",
        {
            "local_path": str(local),
            "remote_dir": remote_dir,
            "remote_name": remote_name,
            "auto_mkdir": True,
            "overwrite": True,
        },
    )


def _push_delete(rel: str) -> None:
    if _use_depot():
        _http("POST", f"/api/depot/delete?ws={_depot_ws()}",
              {"paths": [_depot_path(rel)], "description": f"删除笔记 {rel}"})
        return
    _http("POST", "/api/files/delete", {"paths": [_remote_path(rel)]})


def _push_worker() -> None:
    while True:
        action, rel = _q.get()
        try:
            if not bool(_cfg_get("push_on_change")):
                continue
            if action == "upsert":
                if _local_path(rel).is_file():
                    _push_upsert(rel)
            elif action == "delete":
                _push_delete(rel)
        except Exception as exc:
            lprint(f"[cloud_sync] 推送失败 {action} {rel}: {exc}")
        finally:
            _q.task_done()


def _on_note_change(action: str, rel: str) -> None:
    if not bool(_cfg_get("push_on_change")):
        return
    _q.put((action, rel))


def push_all(notes_root: Path | None = None) -> dict[str, Any]:
    """一次性把本地全部笔记上传到远端（覆盖写，含子目录）。用于「全部同步到百度云」。

    返回统计：{total, ok, fail:[{path,error}], remote_base}。
    不依赖连续同步线程是否已启动（可独立调用）；网盘服务未就绪时抛异常。
    """
    global _notes_root
    root = Path(notes_root) if notes_root is not None else (_notes_root or Path(""))
    if not root or f"{root}" == ".":
        raise RuntimeError("未初始化 notes_root，请传入 notes_root 或先调用 start")
    _notes_root = Path(root)
    files = [p for p in file_store.iter_note_files(_notes_root) if p.is_file()]
    total = len(files)
    ok = 0
    fails: list[dict[str, str]] = []
    for p in files:
        rel = p.relative_to(_notes_root).as_posix()
        try:
            _push_upsert(rel)
            ok += 1
            lprint(f"[cloud_sync] 推送 {rel}")
        except Exception as exc:
            lprint(f"[cloud_sync] 推送失败 {rel}: {exc}")
            fails.append({"path": rel, "error": str(exc)})
    rb = ""
    try:
        rb = _remote_base()
    except Exception:
        rb = ""
    return {"total": total, "ok": ok, "fail": fails, "remote_base": rb}


def _is_prod() -> bool:
    """公网部署机标记：系统级环境变量 Lugwit_deploy=1。0/缺省 = 开发机。"""
    return os.environ.get("Lugwit_deploy", "0").strip().lower() in ("1", "true", "yes", "on")


def note_browser_link(rel: str, request: Any | None = None) -> dict[str, str]:
    """返回某笔记在 lugwit_baidu_netdisk 网页端的文件预览地址。

    网盘 /files 页支持 ?dir=<目录>&open=<完整路径> 深链：加载目录后自动弹出
    该文件的预览视图（文本/图片/音视频等）。返回笔记的直开预览地址。

    files_base 生成顺序（由系统级环境变量 Lugwit_deploy 区分开发机/公网部署机）：
      公网（Lugwit_deploy=1）：强制按请求 Host 动态拼接 scheme://host + files_prefix，忽略 files_base
      开发机（0/缺省）：显式 files_base 优先（本机端口拆分时用）；未配置回退动态
      均不可得 -> 空（url == ""，前端隐藏）
    """
    rel = (rel or "").replace("\\", "/").strip("/")
    remote_path = ""
    try:
        base = _remote_base()
        remote_path = (base + "/" + rel).replace("//", "/") if base else ""
    except Exception:
        remote_path = ""
    folder = remote_path.rsplit("/", 1)[0] if remote_path else ""
    dynamic = ""
    if request is not None:
        try:
            scheme = str(getattr(request.url, "scheme", "") or "http")
            host = str(request.headers.get("host") or "").strip()
            prefix = str(_cfg_get("files_prefix") or "/baidu").strip().rstrip("/")
            if host:
                dynamic = f"{scheme}://{host}{prefix}"
        except Exception:
            dynamic = ""
    if _is_prod():
        # 公网部署机：强制按请求 Host 动态拼接，忽略 files_base（防止误带本机 localhost）
        files_base = dynamic
    else:
        # 开发机：显式 files_base 优先；未配置时回退动态
        files_base = str(_cfg_get("files_base") or "").strip().rstrip("/") or dynamic
    url = ""
    if files_base and folder:
        if _use_depot():
            # 新服务：直接开 depot 页面的该文件（页面支持 ?path= 深链，落到「预览」标签）
            url = f"{files_base}/depot?path={quote(_depot_path(rel), safe='')}"
        else:
            url = (
                f"{files_base}/files?dir={quote(folder, safe='')}"
                f"&open={quote(remote_path, safe='')}"
            )
    return {"url": url, "remote_path": remote_path, "folder": folder}


# ── 拉取（云端 → 本地） ─────────────────────────────────────────

def _register_pulled(rel: str) -> bool:
    if not bool(_cfg_get("register_pulled")):
        return False
    owner = str(_cfg_get("sync_owner") or "").strip()
    if not owner or _db_path is None:
        return False
    conn = dbmod.connect(_db_path)
    try:
        if note_access.is_registered(conn, rel):
            return False
        note_access.register_note(conn, rel, owner)
        return True
    finally:
        conn.close()


def _list_recursive(base: str) -> list[dict[str, Any]]:
    """递归列举远端 base 下的全部文件条目（含 server_mtime/fs_id/path）。"""
    qs = urllib.parse.urlencode({"dir": base})
    data = _http("GET", f"/api/files/list_recursive?{qs}")
    if not isinstance(data, dict):
        raise RuntimeError("递归列举失败：服务未就绪或返回异常")
    if "items" not in data:
        raise RuntimeError(f"递归列举返回异常: {data}")
    return [x for x in data.get("items") or [] if isinstance(x, dict)]


def _pull_once() -> int:
    """单轮拉取：返回下载/新建文件数。"""
    remote_base = _remote_base()
    remote: dict[str, dict[str, Any]] = {}
    for it in _list_recursive(remote_base):
        fp = str(it.get("path") or "")
        rel = _rel_from_base(remote_base, fp)
        if not rel:
            continue
        # depot dir 镜像里 <父目录>/.versions/<名>/vNNN/ 是历史快照，不是笔记
        if ".versions/" in rel or rel.startswith(".versions/"):
            continue
        fsid = it.get("fs_id")
        if not fsid:
            continue
        remote[rel] = {
            "fs_id": int(fsid),
            "server_mtime": int(it.get("mtime") or 0),
            "path": fp,
        }
    if not remote:
        return 0

    done = 0
    for rel, meta in remote.items():
        lp = _local_path(rel)
        require = False
        if not lp.is_file():
            require = True
        else:
            try:
                require = int(lp.stat().st_mtime) < int(meta["server_mtime"])
            except OSError:
                require = True
        if not require:
            continue
        try:
            lp.parent.mkdir(parents=True, exist_ok=True)
            _http(
                "POST",
                "/api/files/download_local",
                {
                    "path": meta["path"],
                    "local_dir": str(lp.parent),
                    "local_name": lp.name,
                },
            )
            try:
                os.utime(lp, (int(meta["server_mtime"]), int(meta["server_mtime"])))
            except Exception:
                pass
            _register_pulled(rel)
            done += 1
        except Exception as exc:
            lprint(f"[cloud_sync] 下载失败 {rel}: {exc}")

    if done:
        file_store.invalidate_brief_cache(_notes_root)  # type: ignore[arg-type]
    return done


def _pull_loop() -> None:
    stop = _pull_stop
    delay = 0.0 if bool(_cfg_get("pull_on_start")) else float(_cfg_get("poll_interval_seconds"))
    while not stop.is_set():
        if stop.wait(max(2.0, float(delay))):
            break
        delay = float(_cfg_get("poll_interval_seconds"))
        with _pull_lock:
            try:
                n = _pull_once()
                global _last_pull_at
                _last_pull_at = time.time()
                if n:
                    lprint(f"[cloud_sync] 拉取完成 {n} 个文件")
            except Exception as exc:
                global _last_error
                _last_error = str(exc)
                lprint(f"[cloud_sync] 拉取失败: {exc}")


# ── 生命周期 ─────────────────────────────────────────

def start(notes_root: Path, db_path: Path) -> bool:
    global _enabled, _notes_root, _db_path
    _notes_root = Path(notes_root)
    _db_path = Path(db_path)
    if not enabled():
        return False
    _enabled = True

    file_store.add_note_change_hook(_on_note_change)  # type: ignore[arg-type]

    worker = threading.Thread(target=_push_worker, name="cloud_sync_push", daemon=True)
    puller = threading.Thread(target=_pull_loop, name="cloud_sync_pull", daemon=True)
    _threads[:] = [worker, puller]
    worker.start()
    puller.start()
    atexit.register(stop)

    folder = str(_cfg_get("app_folder") or "")
    sub = str(_cfg_get("remote_subpath") or "")
    lprint(f"[cloud_sync] 已启用云端镜像 notes_dir={_notes_root} apps/{folder or '?'}/{sub}")
    return True


def stop() -> None:
    global _enabled
    _enabled = False
    file_store.remove_note_change_hook(_on_note_change)  # type: ignore[arg-type]
    _pull_stop.set()


def status() -> dict[str, Any]:
    cfg = _load_cfg()["cloud_sync"]
    apps = ""
    if _enabled:
        try:
            apps = _remote_base()
        except Exception:
            pass
    return {
        "enabled": bool(_enabled),
        "config_enabled": bool(cfg.get("enabled")),
        "apps_root": apps,
        "push_queue_depth": _q.qsize(),
        "last_pull_at": round(_last_pull_at, 3) if _last_pull_at else None,
        "last_error": _last_error or None,
        "poll_interval_seconds": cfg.get("poll_interval_seconds"),
    }


__all__ = ["start", "stop", "enabled", "status", "push_all", "note_browser_link"]
