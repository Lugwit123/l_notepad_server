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
from .routers import accounts, admin, kb, logs, notes, web

from pytracemp import lprint


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
)


def create_dev_app() -> FastAPI:
    """uvicorn --reload 工厂入口：create_app 需要 db_path 参数，
    reloader 子进程只能无参重建，这里用默认/环境变量路径。"""
    return create_app(_parse_db_path(None))


def create_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="L Notepad", version="1.0")
    app.state.db_path = db_path

    # 启动期初始化：建表 + 遗留迁移（用完即关，路由走每请求连接）
    conn = dbmod.connect(db_path)
    dbmod.init_db(conn)

    templates_dir = Path(__file__).resolve().parent / "templates"
    static_dir = Path(__file__).resolve().parent / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    # .dev_mod 热更新：模板不缓存，改 .html 后刷新页面即生效（无需进程重启）。
    # 注：uvicorn 的 --reload 在未装 watchfiles 时退化为 StatReload，只监听 *.py，
    # reload_includes 对模板不生效——所以模板改动靠这里 auto_reload 兜底。
    if os.environ.get("L_DEV_MOD") == "1":
        templates.env.auto_reload = True
    app.state.templates = templates

    notes_root = paths.notes_dir()
    file_store.ensure_root(notes_root)
    app.state.notes_root = notes_root

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

    # ── 响应压缩（长列表 / 大笔记的 HTML 传输体积可降 ~70%）──
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        # Skip CSP for auto-generated API docs (Swagger UI / ReDoc load CDN resources).
        if request.url.path in ("/docs", "/redoc"):
            return resp
        # Minimal CSP: keep scripts local (but allow inline scripts in existing templates).
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
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
    app.include_router(logs.router)
    app.include_router(admin.router)
    app.include_router(accounts.router)
    app.include_router(kb.router)
    app.include_router(web.router)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L Notepad backend server")
    # 默认仅监听回环：对外暴露需显式指定 --host 或 L_NOTEPAD_HOST（生产走 nginx 反代 /note）
    parser.add_argument("--host", default=os.environ.get("L_NOTEPAD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("L_NOTEPAD_PORT", "8765")))
    parser.add_argument("--db", default=None, help="sqlite db path (default: package data/notepad.sqlite3)")
    parser.add_argument("--log-level", default=os.environ.get("L_NOTEPAD_LOG_LEVEL", "info"))
    parser.add_argument(
        "--reload",
        action="store_true",
        # 优先级：L_NOTEPAD_RELOAD > L_DEV_MOD（wuwo 特殊包名 .dev_mod 注入）
        default=os.environ.get("L_NOTEPAD_RELOAD", "").strip() in {"1", "true", "True", "yes", "YES"}
        or os.environ.get("L_DEV_MOD") == "1",
        help="Enable auto-reload (dev only; auto-on with wuwo .dev_mod)",
    )
    args = parser.parse_args(argv)

    if args.reload:
        # reload 需要传 import string + factory（app 对象无法在 reloader 子进程重建）；
        # 只监视本包源码目录，排除日志/临时/测试文件避免重启风暴。
        # reload_includes 覆盖 uvicorn 默认的仅 *.py：装了 watchfiles 时，
        # .dev_mod 热更新即可兼容模板/静态/CSS/JS 等所有前端文件改动。
        # （未装 watchfiles 时 uvicorn 退化为 StatReload，此配置不生效，
        #   模板改动由 create_app 里的 Jinja auto_reload 兜底，见上。）
        uvicorn.run(
            "l_notepad_server.backend_server:create_dev_app",
            factory=True,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parent)],
            reload_includes=[
                "*.py", "*.html", "*.css", "*.js", "*.json", "*.svg", "*.mmd",
            ],
            reload_excludes=[
                "*.log", "logs/*", "*test*", ".*", ".py[cod]", ".sw.*", "~*",
                "__pycache__/*",
            ],
        )
    else:
        app = create_app(_parse_db_path(args.db))
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
