# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path
from typing import Any

# 统一使用 pytracemp lprint 并强制 logging 模式（标准日志格式：时间-级别-消息，无源码跟踪）。
# 注意：环境里 Lugwit_Debug 可能已被预置为 'true'（inspect），这里必须强制覆盖。
os.environ["Lugwit_Debug"] = "logging"

import sys

# asyncpg 与 Windows ProactorEventLoop 不兼容（WinError 64），强制使用 Selector 事件循环
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

import uvicorn

from . import auth as authmod
from . import db as dbmod
from . import file_store
from . import note_access
from . import paths
from . import search_index
from . import search_vec
from .routers import accounts, admin, kb, logs, notes, search, web

from pytracemp import lprint

from l_app_ready.hotreload_service import PORT_ENV, SrcWatchService


def _parse_db_path(value: str | None) -> Path:
    if value:
        return Path(value)
    env_path = os.environ.get("L_NOTEPAD_DB")
    if env_path:
        return Path(env_path)
    return dbmod.default_db_path()


# token 验证结果短 TTL 缓存：首屏/静态资源会触发大量并发请求，
# 避免每个请求都同步往返 Auth Service（原实现会阻塞事件循环导致启动卡顿）。
_VERIFY_CACHE_TTL_S = 5.0
_verify_cache: dict[str, tuple[float, Any]] = {}


# 未登录可访问的路径前缀（页面跳登录，API 401）
PUBLIC_PATHS = (
    "/api/auth/login",
    "/api/auth/logout",
    "/login",
    "/static",
    "/favicon.svg",
    "/favicon.ico",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/health",
    # 热重载开关（本机运维接口，与 /api/health 同级）：登录态会把主页/脚本的探活请求
    # 302 到登录页，导致"开关看不见状态"。只读状态 + 受 .dev_mod 硬门控，暴露无风险。
    "/__dev__/src_watch",
)

# ── 本机直连免 token ──────────────────────────────────────
# 只对「直连后端（未经反代）且对端是回环地址」的**只读**请求免鉴权，便于本机脚本/curl/
# 托盘等直接调 `/api/search`、`/api/kb/**` 调试；身份按 guest 走，权限过滤照旧生效。
#
# 安全性依赖两点：
#   1) 必须没有 X-Real-IP / X-Forwarded-For —— nginx 反代会写入真实客户端 IP
#      （`proxy_set_header X-Real-IP $remote_addr`），所以经反代的请求**从不解禁**；
#      否则同机 nginx 会把所有远程请求都伪装成 127.0.0.1。
#   2) 必须同时满足对端 IP 是回环，因此即使有人把后端绑到 0.0.0.0，远程直连也不会免鉴权。
# 关闭方式：L_NOTEPAD_LOCAL_NO_AUTH=0。
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _local_no_auth_enabled() -> bool:
    return os.environ.get("L_NOTEPAD_LOCAL_NO_AUTH", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _is_local_direct(request: Request) -> bool:
    """本机直连后端（未经反代 + 对端回环）。"""
    if request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For"):
        return False
    host = (request.client.host if request.client else "") or ""
    return host in _LOCAL_HOSTS


def create_dev_app() -> FastAPI:
    """uvicorn --reload 工厂入口：create_app 需要 db_path 参数，
    reloader 子进程只能无参重建，这里用默认/环境变量路径。"""
    return create_app(_parse_db_path(None))


# ── 源码热重载（统一 L_SRC_WATCH，替代 uvicorn --reload）──────────────────────
# 单进程运行；改 .py 自重启，改 templates/*.html 靠 Jinja auto_reload 刷新即变。
# **硬门控：必须带 .dev_mod 才开**（详见 Rez-Docs/src_hot_reload_源码热重载与主页常驻.md）。
# 注：数据/日志都落在用户目录（`~/.Lugwit/l_notepad`、`D:\Temp\Log`），不写包源码目录；
# `__pycache__` 由 DefaultExcludeDirs 排除，不会出现"写文件→重启→再写"死循环。
_PORT = int(os.environ.get(PORT_ENV) or 8765)


def _runtime_dir() -> Path:
    override = os.environ.get("L_NOTEPAD_RUNTIME")
    p = Path(override) if override else Path.home() / ".lugwit" / "l_notepad_server" / "runtime"
    p.mkdir(parents=True, exist_ok=True)
    return p


_sw = SrcWatchService(
    module="l_notepad_server.backend_server",
    pkg="l_notepad_server",
    alias="l_notepad_api",
    port=_PORT,
    watch_root=Path(__file__).resolve().parent,
    runtime_dir=_runtime_dir(),
    label="l_notepad",
)
_sw.start()


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="L Notepad", version="1.0")
    app.state.db_path = db_path

    # 启动期初始化：建表 + 遗留迁移（用完即关，路由走每请求连接）
    conn = dbmod.connect(db_path)
    dbmod.init_db(conn)

    templates_dir = Path(__file__).resolve().parent / "templates"
    static_dir = Path(__file__).resolve().parent / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    # 热重载开启时模板不缓存，改 .html 后刷新页面即生效（无需进程重启）
    templates.env.auto_reload = _sw.is_enabled()
    app.state.templates = templates
    _sw.jinja_env = templates.env   # 之后在页面上切开关时 auto_reload 会跟着变
    _sw.mount(app)                  # GET/POST /__dev__/src_watch

    notes_root = paths.notes_dir()
    file_store.ensure_root(notes_root)
    app.state.notes_root = notes_root

    # 搜索索引：订阅笔记变更通知（索引在首次查询时惰性增量构建）
    search_index.install()
    # 向量（语义检索）表：vec_docs / vec_chunks，未启 embedding 时只是空表
    try:
        search_vec.init_schema(conn)
        search_vec.bind_db_path(db_path)
    except Exception:
        pass

    # ── 百度网盘云端镜像（可选）：笔记本地 + 云端双向同步 ──
    try:
        from . import cloud_sync

        if cloud_sync.enabled():
            cloud_sync.start(notes_root, db_path)
    except Exception as _e:  # noqa: BLE001 - 启动不因云同步失败而阻塞
        lprint(f"[l_notepad] 云端镜像启动跳过: {_e}")

    # 迁移遗留旧笔记：注册给 admin01 并设为全部共享（幂等，只处理未注册的）
    try:
        _migrated = note_access.migrate_legacy_notes(conn, notes_root, "admin01")
        for _p in _migrated:
            note_access.set_public(conn, _p, True, "read")
        if _migrated:
            lprint(f"[l_notepad] 已迁移 {len(_migrated)} 条旧笔记为全部共享")
    except Exception as _e:  # noqa: BLE001 - 迁移失败不阻塞启动
        lprint(f"[l_notepad] 旧笔记迁移跳过: {_e}")
    finally:
        conn.close()

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.state.static_dir = static_dir   # 静态资源指纹（?v=）用，见 routers/deps.static_url

    # ── 响应压缩（长列表 / 大笔记的 HTML 传输体积可降 ~70%）──
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        # 静态资源 / 页面不走启发式缓存：客户端是固定 profile 的 QtWebEngine，
        # 缓存住旧 app.js 会让顶栏菜单点不开（改为每次用前 revalidate，正常走 304）。
        if request.url.path.startswith("/static/") or str(
            resp.headers.get("content-type", "")
        ).startswith("text/html"):
            resp.headers.setdefault("Cache-Control", "no-cache")
        # Skip CSP for auto-generated API docs (Swagger UI / ReDoc load CDN resources).
        if request.url.path in ("/docs", "/redoc"):
            return resp
        # Minimal CSP: keep scripts local (but allow inline scripts in existing templates).
        # connect-src 额外放行本机托盘 ExecServer（知识库「本机模式」经它读写浏览器所在机器的目录）。
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self' http://127.0.0.1:19527 http://localhost:19527; "
            "font-src 'self' data:;",
        )
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    # ── 登录鉴权：接入 lugwit_auth（本地直连 Auth Service），未登录拦截 ──
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PATHS):
            return await call_next(request)
        # 本机直连（127.0.0.1:8765，未经反代）的只读请求免 token → 身份按 guest 处理，
        # 仍走 note_access 权限过滤（看不到他人笔记；知识库对所有登录用户可见）。
        if (
            request.method in ("GET", "HEAD")
            and _local_no_auth_enabled()
            and _is_local_direct(request)
        ):
            return await call_next(request)
        # 优先 Authorization header，其次 cookie（浏览器 reload 场景）
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        if not token:
            token = request.cookies.get("l_notepad_token")
        if token:
            cached = _verify_cache.get(token)
            if cached is not None and time.monotonic() - cached[0] < _VERIFY_CACHE_TTL_S:
                payload = cached[1]
            else:
                cacheable = True
                try:
                    # 放到线程池，避免阻塞 urllib 调用卡死整个事件循环
                    payload = await run_in_threadpool(authmod.verify_token, token)
                except Exception:
                    # Auth Service 暂不可用：按未登录处理（避免中间件 500），不缓存该结果
                    payload = None
                    cacheable = False
                if cacheable:
                    if len(_verify_cache) > 256:
                        # 淘汰最旧条目，避免缓存无限膨胀
                        oldest = min(_verify_cache, key=lambda k: _verify_cache[k][0])
                        _verify_cache.pop(oldest, None)
                    _verify_cache[token] = (time.monotonic(), payload)
            if payload:
                request.state.login_user = payload.get("sub")
                request.state.login_role = authmod.role_int_to_label(payload.get("role"))
                request.state.login_role_int = payload.get("role")
                request.state.login_user_id = payload.get("user_id")
                request.state.login_token = token
                return await call_next(request)
        # 未登录：API 返回 401，页面重定向到登录页（带反代前缀，避免被入口路由劫持）
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未认证，请先登录"}, status_code=401)

        def _root_base(req: Request) -> str:
            base = (req.scope.get("root_path") or "").rstrip("/")
            if base:
                return base
            return (req.headers.get("X-Forwarded-Prefix") or "").strip().rstrip("/")

        rb = _root_base(request)
        return RedirectResponse(f"{rb}/login" if rb else "/login", status_code=302)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        cloud_status: dict[str, Any] = {"enabled": False}
        try:
            from . import cloud_sync

            cloud_status = cloud_sync.status()
        except Exception:
            pass
        return {"ok": True, "cloud_sync": cloud_status}

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon_svg() -> FileResponse:
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_ico() -> FileResponse:
        # Reuse the SVG icon to avoid browser 404s.
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    # ── 路由分域注册（原先 789 行单文件按 domain 拆分）──
    app.include_router(notes.router)
    app.include_router(notes.meta_router)
    app.include_router(search.router)
    app.include_router(logs.router)
    app.include_router(admin.router)
    app.include_router(accounts.router)
    app.include_router(kb.router)
    app.include_router(web.router)

    return app


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    # 独立重启执行进程：只做「停旧 + 起新」，不做服务初始化
    if _sw.handle_restart_argv(argv):
        return 0
    parser = argparse.ArgumentParser(description="L Notepad backend server")
    # 默认仅监听回环：对外暴露需显式指定 --host 或 L_NOTEPAD_HOST（生产走 nginx 反代 /note）
    parser.add_argument("--host", default=os.environ.get("L_NOTEPAD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_PORT)
    parser.add_argument("--db", default=None, help="sqlite db path (default: package data/notepad.sqlite3)")
    parser.add_argument("--log-level", default=os.environ.get("L_NOTEPAD_LOG_LEVEL", "info"))
    args = parser.parse_args(argv)
    _sw.port = args.port   # 端口以实际启动参数为准（重启时按它找旧进程）

    # 热重载改由进程内 SrcHotReload 负责（统一开关 L_SRC_WATCH）：不再使用 uvicorn --reload
    # （它生成的 reload 孤儿 worker 命令行不含 app 名，.solo 守卫看不见 → 双实例抢端口）。
    app = create_app(_parse_db_path(args.db))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
