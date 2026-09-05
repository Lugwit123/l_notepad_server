# -*- coding: utf-8 -*-

name = "l_notepad_server"
version = "999.0"
description = "L Notepad FastAPI backend: web UI + REST + knowledge base (independent of PC client)"
authors = ["Lugwit Team"]

# NOTE:
# - 服务端只依赖 Web 栈（fastapi/uvicorn/jinja2/pydantic），不含 PySide6/桌面库。
# - 认证经 lugwit_auth（1027）HTTP 接入；客户端/服务端通过 REST + token 契约通信。
# - l_qframelesswindow 提供 ServerConfigStore（server_config 复用同一套持久化）。
# - lugwit_baidu_netdisk 仅经其 Web 服务接口（HTTP）接入（cloud_sync 云端镜像/网盘链接），
#   不直接依赖该包，避免服务端拉入其重依赖。
requires = [
    "python-3.12.10",
    "fastapi",
    "uvicorn",
    "jinja2",
    "pydantic",
    "python_multipart",
    "l_qframelesswindow",
    "pytracemp",
    "watchfiles",
]

build_command = False
cachable = True
relocatable = True


def commands():
    env.PYTHONPATH.prepend("{root}/src")
    env.L_NOTEPAD_ROOT = "{root}"
    env.PYTHONIOENCODING = "utf-8"

    # 服务端入口（8765，经 nginx /note 反代；本地直连 127.0.0.1:8765）
    alias("l_notepad_api", "python -m l_notepad_server.backend_server")
    # 热更新模式：显式开启 uvicorn --reload（监视本包源码目录）
    alias("l_notepad_api_reload", "python -m l_notepad_server.backend_server --reload")
