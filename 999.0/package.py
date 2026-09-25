# -*- coding: utf-8 -*-
# === wuwo doc_pkg BEGIN v7 (auto-generated, do not edit) ===
# 包：l_notepad_server 999.0  L Notepad FastAPI backend: web UI + REST + knowledge base (independent of PC client)
# 依赖：python-3.12.10, fastapi, uvicorn, jinja2, pydantic, python_multipart, l_qframelesswindow
#    pytracemp, watchfiles
# 提供：PYTHONPATH {root}/src；env L_NOTEPAD_ROOT；PYTHONIOENCODING=utf-8
# 入口：l_notepad_api, l_notepad_api_reload
# 用法：wuwo l_notepad_server 进入该包环境；wuwor l_notepad_server -- l_notepad_api 直接调用别名
# 目录：包自有代码根：src/l_notepad_server/
#    自有子目录：doc、routers、static、templates
#    不要在源码根下盲搜全量文件，先按上面目录定位；第三方库的问题不在本包职责内。
# 查文档：全量搜索（笔记+全部知识库）：curl "http://127.0.0.1:8765/api/search?q=<关键词>&limit=10" → hits[].kb_name / rel
#     只搜本仓文档：curl "http://127.0.0.1:8765/api/kb/rez_pkg/search?q=<关键词>&limit=10"
#     读正文：curl "http://127.0.0.1:8765/api/kb/<kb_name>/workspace/file?path=<rel>"（参数名 path，不是 rel）
#     本机直连 8765 免 token（仅 GET，勿经 nginx）；q 要 URL 编码；score < 1.0 多为向量噪声
#     接口全表与踩坑见 D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez_pkg/l_notepad_搜索接口使用文档.md
# 跑代码：远程连上宿主程序，用宿主解释器跑代码（依赖与 Qt 事件循环齐备），别自己另起解释器
#     探活：curl http://127.0.0.1:8764/status（多实例逐个试 8764 / 8769；editor_available=true 才可用）
#     执行：curl -H "Content-Type: application/json" -d "{\"code\": \"print(1)\"}" http://127.0.0.1:8764/execute
#     长代码先落盘再 exec(open(r'<abs.py>', encoding='utf-8').read())，避免 JSON 转义地狱
#     其它工具：/execute_async 异步、/upload /upload_folder /download 传文件、/tools 列 agent 工具
#     UI 自动化（Qt 版 Playwright）：/ui/tree 控件树、/ui/locate 定位、/ui/action click|set_text|press_key、/ui/wait auto-wait、/ui/screenshot 截图
#     全表见 D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez_pkg/l_script_editor.md
# 建包：新建/改包前先读 D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez包创建和启动指导文档.md
#    覆盖：目录布局 <包名>/<版本>/package.py、requires 写法、alias/env、变体哈希、修饰符与启动方式
# 规范：999.0 源码即环境（改源码即生效，无需 build）；依赖写进 requires 由 wuwo 自动补齐
#    修饰符 .dev_mod / .solo / .soloignore / .script_server 与建包规范见 Rez-Docs/Rez包创建和启动指导文档.md
# === wuwo doc_pkg END ===

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

    # rez 源码包货架：搜索页「要搜索哪些包」的勾选来源
    # （`{root}` = <trayapp>/rez-package-source/<包>/<版本>，向上三级即 trayapp）。
    # 注意：这里必须把 `{root}` 交给 rez 去展开——先 os.path.abspath 会把 `{root}`
    # 当普通目录名规范化掉，结果指到 trayapp 的上一级。
    env.L_NOTEPAD_PKG_ROOT = "{root}/../../../rez-package-source"

    # 本机 cross-encoder 重排服务（llama.cpp llama-server --reranking；启动脚本见
    # D:/Tools/llama.cpp/start_rerank.bat）。语义召回分数区分度低，重排才分得开候选。
    # 部署机没有这个服务：连不上会自动降级回原融合排序（不报错、不返回空），
    # 所以按 Lugwit_deploy 关掉，省掉探活开销（wuwo 常把它设成 "0"，要按真假值判断）。
    deploy = (getenv("Lugwit_deploy") or "").strip().lower() in ("1", "true", "yes", "on")
    if not deploy:
        env.L_NOTEPAD_RERANK_URL = "http://127.0.0.1:11435"
        # 本机是纯 CPU 交叉编码器（bge-reranker-v2-m3 Q8，实测 ~1.5ms/token）：
        # 候选数与候选长度直接决定耗时。8 个候选 × 300 字符 ≈ 900 token ≈ 1.2s，
        # 是「能分得开候选」与「检索不至于等好几秒」之间的折中。
        env.L_NOTEPAD_RERANK_TOP_N = "8"
        env.L_NOTEPAD_RERANK_MAX_CHARS = "300"
        env.L_NOTEPAD_RERANK_TIMEOUT_S = "8"

    # 服务端入口（8765，经 nginx /note 反代；本地直连 127.0.0.1:8765）
    alias("l_notepad_api", "python -m l_notepad_server.backend_server")
    # 与 l_notepad_api 相同：热重载已改由进程内 SrcHotReload 统一负责
    # （统一开关 L_SRC_WATCH，需 .dev_mod 启动；见 Rez-Docs/src_hot_reload…）：
    # 原 --reload 参数已移除，别名保留只为兼容主页卡片的「♻ 热更新」按钮。
    alias("l_notepad_api_reload", "python -m l_notepad_server.backend_server")
