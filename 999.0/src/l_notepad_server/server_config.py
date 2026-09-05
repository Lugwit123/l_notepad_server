# -*- coding: utf-8 -*-
"""服务器地址配置：持久化到 ~/.Lugwit/l_notepad/server_config.json。

读写逻辑由 l_qframelesswindow 标题栏库的 ServerConfigStore 提供（标题栏
「服务器设置」对话框写出的就是同一份配置）；本模块仅绑定数据目录、
默认值与环境变量映射，并保留模块级便捷函数，供各消费方直接使用。

优先级：UI 持久化配置 > 环境变量 > 默认值。
host 默认值由系统级环境变量 Lugwit_deploy 自动区分：
    开发机（缺省）        → 本机 nginx http://127.0.0.1:8080
    公网部署机（Lugwit_deploy=1）→ 生产 nginx 统一入口 http://121.196.144.88:8080
    auth_route = /api/v1/auth（登录 / verify / me 端点拼在 auth_url 之后，
                               公网登录路由即 http://121.196.144.88:8080/api/v1/auth/login）
    api_url / log_server_url = <host>/note（location /note/ 剥前缀转发到笔记后端 8765）

注意：本模块不得依赖 PySide6/Qt，以便非 UI 模块（auth、backend_server 等）直接使用。
"""
from __future__ import annotations

import os
from pathlib import Path

from l_qframelesswindow.server_config import ServerConfigStore

from . import paths

# 服务器类型 -> (默认地址, 环境变量名)
# host 由系统级环境变量 Lugwit_deploy 自动区分（开发机=本机 nginx 127.0.0.1:8080；
# 公网部署机=生产 nginx 8080 统一入口，location /api/v1/ → lugwit_auth，
# /note/ 剥前缀转发到笔记后端 8765）。8765 只监听 127.0.0.1，不对外暴露端口。
def _is_prod() -> bool:
    """公网部署机标记：系统级环境变量 Lugwit_deploy=1。0/缺省 = 开发机。"""
    return os.environ.get("Lugwit_deploy", "0").strip().lower() in ("1", "true", "yes", "on")


_HOST_PREFIX = "http://121.196.144.88:8080" if _is_prod() else "http://127.0.0.1:8080"
_DEFAULTS = {
    "auth_url": _HOST_PREFIX,                      # 认证服务：nginx 入口（/api/v1/ → lugwit_auth）
    "auth_route": "/api/v1/auth",                  # 认证路由（login / verify / me 端点前缀）
    "api_url": _HOST_PREFIX + "/note",             # 笔记 / 账号 API：nginx 入口 + /note 路由
    "log_server_url": _HOST_PREFIX + "/note",      # 远端日志服务：同上
}
_ENV_MAP = {
    "auth_url": "LUGWIT_AUTH_URL",
    "auth_route": "LUGWIT_AUTH_ROUTE",
    "api_url": "L_NOTEPAD_API_URL",
    "log_server_url": "L_NOTEPAD_LOG_SERVER",
}

_store = ServerConfigStore(
    data_dir=paths.data_root(),
    defaults=_DEFAULTS,
    env_map=_ENV_MAP,
)


def store() -> ServerConfigStore:
    """标题栏「服务器设置」使用的配置存储实例。"""
    return _store


def config_file() -> Path:
    """服务器配置文件路径。"""
    return _store.config_file()


def auth_url() -> str:
    """认证服务地址。"""
    return _store.auth_url()


def auth_route() -> str:
    """认证路由（登录 / token / me 等端点前缀，如 /api/v1/auth）。"""
    return _store.auth_route()


def api_url() -> str:
    """笔记 / 账号 API 地址（默认 http://127.0.0.1:8080/note：nginx 入口 + /note 路由）。"""
    return _store.api_url()


def log_server_url() -> str:
    """远端日志服务地址（默认与 api_url 同：nginx 8080 入口 + /note 路由）。

    纯 host（如 121.196.144.88）时自动补 http:// 与端口，兼容旧写法。
    """
    val = _store.get_raw("log_server_url")
    if not val.startswith(("http://", "https://")):
        port = os.environ.get("L_NOTEPAD_PORT", "8765")
        return f"http://{val}:{port}"
    return val


def save(
    auth: str | None = None,
    api: str | None = None,
    log_server: str | None = None,
    route: str | None = None,
) -> dict:
    """保存服务器配置（None 表示保留当前值），返回合并后的完整配置。"""
    return _store.save(
        auth_url=auth,
        auth_route=route,
        api_url=api,
        log_server_url=log_server,
    )