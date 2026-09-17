<!-- === wuwo doc_pkg BEGIN v7 (auto-generated, do not edit) === -->

## 包信息（`wuwo doc_pkg` 自动生成，勿手改块内）

| 项 | 内容 |
| --- | --- |
| 包 | `l_notepad_server 999.0` — L Notepad FastAPI backend: web UI + REST + knowledge base (independent of PC client) |
| 依赖 | `python-3.12.10`<br>`fastapi`<br>`uvicorn`<br>`jinja2`<br>`pydantic`<br>`python_multipart`<br>`l_qframelesswindow`<br>`pytracemp`<br>`watchfiles` |
| 提供 | PYTHONPATH {root}/src<br>env L_NOTEPAD_ROOT<br>PYTHONIOENCODING=utf-8 |
| 入口 | `l_notepad_api`<br>`l_notepad_api_reload` |
| 用法 | `wuwo l_notepad_server` 进入该包环境<br>`wuwor l_notepad_server -- l_notepad_api` 直接调用别名 |

**目录**

- 包自有代码根：src/l_notepad_server/
- 自有子目录：doc、routers、static、templates
- 不要在源码根下盲搜全量文件，先按上面目录定位；第三方库的问题不在本包职责内。

**查文档（本机 l_notepad 检索接口）**

```bash
# 全量搜索（笔记 + 全部知识库）→ hits[].kb_name / rel
curl "http://127.0.0.1:8765/api/search?q=<关键词>&limit=10"
# 只搜本仓文档
curl "http://127.0.0.1:8765/api/kb/rez_pkg/search?q=<关键词>&limit=10"
# 读正文（参数名 path，不是 rel）
curl "http://127.0.0.1:8765/api/kb/<kb_name>/workspace/file?path=<rel>"
```

本机直连 8765 免 token（仅 GET，勿经 nginx）；`q` 要 URL 编码；`score < 1.0` 多为向量噪声。

接口全表与踩坑见 `D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez_pkg/l_notepad_搜索接口使用文档.md`。

**跑代码（脚本编辑器远程执行）**

远程连上**宿主程序**，直接用宿主解释器执行代码 —— 依赖（PySide6/rez）与 Qt 事件循环都在，适合在真实进程里验证改动，别自己另起解释器。

```bash
# 探活（多实例逐个试 8764 / 8769；editor_available=true 才可用）
curl http://127.0.0.1:8764/status
# 同步执行
curl -H "Content-Type: application/json" -d "{\"code\": \"print(1)\"}" http://127.0.0.1:8764/execute
# 有哪些 agent 工具
curl http://127.0.0.1:8764/tools
```

长代码先落盘再 `exec(open(r'<abs.py>', encoding='utf-8').read())`，避免 JSON 转义地狱。

除执行外还有：`/execute_async` 异步提交（返回 `request_id`）、`/upload` `/upload_folder` `/download` 传文件、`/tools` 列 agent 工具清单。

UI 自动化 = **Qt 版 Playwright**：`/ui/tree` 导控件树 → `/ui/locate` 解析定位器 → `/ui/action`（`click` / `set_text` / `press_key` / `select`）→ `/ui/wait` auto-wait → `/ui/screenshot` 控件截图。

接口全表与参数见 `D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez_pkg/l_script_editor.md`。

**建包**

新建或改 Rez 包前先读 `D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez包创建和启动指导文档.md`（目录布局 `<包名>/<版本>/package.py`、`requires` 写法、`alias`/`env`、变体哈希、修饰符与启动方式）。

**规范**

- 999.0 源码即环境（改源码即生效，无需 build）；依赖写进 requires 由 wuwo 自动补齐
- 修饰符 .dev_mod / .solo / .soloignore / .script_server 与建包规范见 Rez-Docs/Rez包创建和启动指导文档.md

<!-- === wuwo doc_pkg END === -->
