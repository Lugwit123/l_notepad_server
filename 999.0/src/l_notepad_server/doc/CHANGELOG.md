# L Notepad 更新日志

## v3.4.0 (2026-09-25)

### 「判断依据」：每条命中为什么排在这里（2026-09-25 晚）
- 🧾 每条命中新增 `explain` 字段（`_explain` / `_annotate_ranks`）：分项表（权重×取值=得分）、
  逐词块明细（**IDF 权重 + 出现次数**）、被判泛词（权重 0，不参与打分）、未命中词块、
  名次 `rank/of`、排序主序 `order_by`、与**下一条**的分差与主因（`vs_next`）、一句话结论 `summary`
- 🔘 结果卡片新增「判断依据」按钮 → 弹出对话框（`static/app.js` 的 `LN.renderSearchHits` +
  `.ln-why` 样式在 `app.css`）：顶栏弹窗、搜索页、「试搜」三处共用，纯前端渲染，无额外请求
- 用途不止给人看：`explain.summary` 可直接作为 Agent「为什么召回它」的依据

### 「要搜索哪些包」（2026-09-25 晚）
- 🔤 搜索页新增 **rez 源码包勾选**：`GET /api/search/code_packages` 列出货架（`L_NOTEPAD_PKG_ROOT`，
  默认 `<trayapp>/rez-package-source`）下带 `package.py` 的包（本机 54 个），勾选后只在这些包里搜代码；
  `GET /api/search?packages=a,b` 只作用于 `source=code`（笔记 / 知识库不受影响）；**默认不勾选 = 不限**
- 🗂 包是**手动建索引**的第三类本机库（`kind=pkg`）：`POST /api/search/index_lib {"label":"l_agent_chat"}`
  （实测 133 文件 / 0.7s）；不参与 TTL 自动刷新，全量重建也只重建「建过索引的」包
  （货架全量 3 万+ 文件 / ≈1.9GB，一次铺开会失控）
- 💾 勾选状态存浏览器 `localStorage['ln_search_packages']`；从顶栏进搜索页时自动补进 URL

### 长句召回与排序修正（2026-09-25 晚，口语整句实测驱动）
- 🧩 **长句不再段间 AND**（`build_match_expr`）：段数 > `_MAX_AND_GROUPS`(4) 时改走「全部单元 OR + 覆盖率排序」。
  口语原句 `ctrl+中键呼出…整个电脑都卡很久` 被切成 6–8 段，原来段间 AND 只剩 **2 条**命中（目标文件不在候选里，
  重排也无从下手）；改后同一句 **87 条**候选，目标文件可被重排顶到第 1
- 🔀 **`mode=auto` 长句自动走混合**（`search_auto`）：命中 < `_AUTO_MIN_HITS`(3) **或** 查询本身是长句时改用 hybrid
  （语义 + 重排）。此前 `auto` 只在零命中才回退，长句会停在「lex 的 2 条结果」上
- 实测同一句 `mode=auto`：`mode_used=hybrid`、`total=87`、约 1.5s，**第 1 名 = `folder_favorites_hotkey.py`**（重排 +0.74）

### 新增功能
- 🗃 **手动「创建索引」（本机库）**：搜索页新增「索引管理」面板，按库点「创建索引」即扫该库目录（**含 `.py` 等代码文件**），只写本机索引、**不触发 depot 上传**
  - `GET /api/search/index_libs`：可建索引的本机库（`kind=code` 代码库根 / `kind=kbws` 知识库工作区）+ 已索引文档数 + 最近扫描状态
  - `POST /api/search/index_lib`（管理员，`{"label": "...", "embed": true}`）：只扫该库 → 返回 `files/touched/duration_ms/capped`，随后台嵌入向量
  - 知识库工作区走**本机索引**而非归档：`workspace_sync` 上传白名单仍是文档类型（`.py` 不会被动上传），工作区文件也不会被归档同步删掉；代码库根仍随 `SCAN_TTL_S` 自动刷新，工作区只在手动点按时重建（手动全量重建也会带上它们）
- 🧠 **代码库参与向量语义**：`source=code` 纳入嵌入流程（读本机文件，键 `code:<label>:<rel>`），口语症状（"卡很久/卡顿"）现在能语义召回代码 —— 实测原句命中目标文件 `vec=0.63`
  - 单篇嵌入失败改为**跳过并计数**（`_EMBED_RETRY_MAX=3` 后放弃），一篇坏文档不再卡住整轮；`stats.vec.by_source` / `dropped` 可见
  - 内存向量缓存分额度：代码块上限 `L_NOTEPAD_VEC_CODE_CHUNKS`（默认 8000），避免代码把笔记/知识库挤出缓存
- 🔤 **关键词抽取与泛词抑制**：
  - 路由关键词保留**关键单字**（`卡/慢/死`，FTS 前缀匹配）、同义扩展（`卡 ↔ 卡顿/卡死/阻塞`，`_SYNONYMS`）；bigram 只在**整块都是低信息字**时才丢弃（原来含单个低信息字就丢，「卡很」会被误杀）
  - **IDF 泛词抑制**：`term_idf()` 用 FTS5 词表（`fts5vocab`）算词块文档频率，占比 ≥ 30% 的 repo 泛词（`notepad/client/窗口/程序`）不参与覆盖率/词频/近邻打分，路由层直接剔除（`terms_generic_dropped`）
- 📄 **代码命中体验**：新增只读查看页 `GET /web/code?root=&file=&hl=`（行号 + 命中词高亮，超 5000 行截断）；结果卡片代码库命中加 🧩 与库名徽标；`open_url` 统一指向该页
- 🛡 **代码库体量治理**：`L_NOTEPAD_CODE_MAX_FILES`（默认 20000）/ `L_NOTEPAD_CODE_MAX_BYTES`（默认 512MB）超限即停止扫描并标记 `capped`（截断时**不会**误删索引行）；`CODE_SKIP_DIRS` 增加 `.vs/obj/.ruff_cache/.pytype/__pypackages__/.ipynb_checkpoints/htmlcov`
- 🧭 **`route` 支持本机库**：`sources` 默认 `kb,code`，depth0 用库标签做元数据命中，结果带 `terms_generic_dropped`

### 重排（rerank）落地（2026-09-25）
- 本机部署 llama.cpp `llama-server --reranking`（`D:\Tools\llama.cpp\start_rerank.bat`，端口 11435）+
  `bge-reranker-v2-m3-Q8_0.gguf`；`package.py` 在非 `Lugwit_deploy` 环境注入
  `L_NOTEPAD_RERANK_URL` / `L_NOTEPAD_RERANK_TOP_N=8` / `L_NOTEPAD_RERANK_MAX_CHARS=300` / `L_NOTEPAD_RERANK_TIMEOUT_S=8`
- 新增 `L_NOTEPAD_RERANK_MAX_CHARS`（默认 400）：候选只送「查询词附近」的一窗。
  cross-encoder 成本 ≈ 正比 token 数，块按 900 字符切分 ≈ 350 token，CPU 上每个候选 0.5s，不截断没法用
- 实测（16C/32T CPU，bge-reranker-v2-m3 Q8）：5 候选 `rerank≈0.6s`、整轮 `hybrid`≈0.85s；
  口语整句「ctrl+中键呼出…整个电脑卡很久」的目标文件 `folder_favorites_hotkey.py` 由第 3 名升到**第 1 名**
- `-ub` 必须调大（启动脚本用 2048）：默认 512 装不下一个 400 字符候选 → 退化成一次打一个候选

### 接口
- 新增 `GET /api/search/index_libs`、`POST /api/search/index_lib`
- `GET /api/search/route`：`sources` 默认由 `kb` 改为 `kb,code`；返回新增 `terms_generic_dropped`
- `GET /api/search/code/file`：`root` 支持知识库工作区标签（不再只限 `code_roots`）
- `GET /api/search/stats` 新增 `code_libs` / `code_max_files` / `code_max_bytes`，`sources[]` 的 code 行带 `kind` / `editable` / `scan`
- `GET /web/search` 新增「索引管理」面板；新增页面 `GET /web/code`

### 行为变化
- 泛词（覆盖率 ≥ 30% 的词块）不再参与相关性打分 → 命中项 `coverage` / `score` 会与旧版不同（更贴近"以罕见词定排名"）
- `route` 默认多返回代码库/工作区候选；Agent 只要知识库请显式 `sources=kb`

## v3.3.0 (2026-09-24)

### 新增功能
- 🔎 **快速选库路由 `GET /api/search/route`**：一段复杂需求 → 知识库级排序（`depth` 0 元数据 / 1 词法按库聚合 / 2 语义摘要向量 / 3 交回 Agent）；毫秒级，带 `budget_ms` 降级、`reason_code` 与结果缓存
- 🗂 **独立全局搜索页 `GET /web/search`**：一次搜「笔记 + 全部知识库 + 代码库」；参数 `mode(auto/lex/hybrid/sem) / sources / kb / rerank / limit / offset`、知识库分面、搜索帮助面板、搜索历史（localStorage）、结果卡片复用 `LN.renderSearchHits`（顶栏弹窗回车 → 此页）
- 🧩 **代码库索引（`source=code`）**：`GET/PUT /api/search/code_roots`（管理员，运行时配置本机目录，保存后自动后台重建）、`GET /api/search/code/file` 只读查看；状态页新增「代码库索引」卡片；扫描按 `CODE_EXTS` 过滤并跳过 `.git/__pycache__/node_modules/...`
- 🔤 `/api/search?mode=auto`：先词法，零命中再回退 hybrid（长句/自然语言不再"无命中"）

## v3.2.0 (2026-09-18)

### 新增功能
- 🔀 **本地交叉编码重排（rerank）**：融合排序之后再对候选块做 cross-encoder 重排，重排分作**排序主序**（原有 `score` 语义不变，重排分只作排序依据与独立字段）；后端走本机 llama.cpp `llama-server --reranking`（`POST /rerank`，失败自动试 `/v1/rerank`）
  - 配置：`L_NOTEPAD_RERANK_URL`（必填，未设置=不启用）/ `L_NOTEPAD_RERANK_MODEL`（空=服务端默认）/ `L_NOTEPAD_RERANK_ENABLED`（默认开）/ `L_NOTEPAD_RERANK_TOP_N`（40）/ `L_NOTEPAD_RERANK_TIMEOUT_S`（3）
  - 优先级：环境变量 > 页面设置（`app_settings.rerank_enabled` / `rerank_model`）> 默认，与 embedding 模型完全同构；URL 只读展示，不接受页面写入
  - 候选：**每篇文档只投递 1 个块**（向量最优块 ∪ 词法候选文档的向量最优块），按融合分降序截断到 `TOP_N`；无块向量的文档不参与重排（保留原分与相对顺序，垫底）
  - 降级：未配置 / 已关闭 / 冷却中 / 连不上 / 超时 / 返回结构不符 / 无候选块 → 本轮**退回原融合排序**，检索不报错、不返回空；连续失败 3 次进入 60s 冷却（冷却期内不发请求），探测结果缓存 60s，成功后自动恢复
  - 单次对比：`GET /api/search?...&rerank=0` 临时关闭；全局关闭时传 `rerank=1` 也不生效
- 🧩 **命中块（chunk）级信息**：命中项新增 `chunk` / `chunk_no` / `chunk_offset` / `rerank`，响应新增 `rerank: {used, model, scored, took_ms, reason}`；摘要**优先取自命中块**（块偏移 → 长前缀 / 首行查找 → 原有「短语优先 → 词块最密集窗口」回退），定位与偏移统一按 `\n` 归一化（CRLF 正文也对得上）；高亮仍在正文上产出、转义规则不变
  - `vec_chunks` 新增块偏移列 `start`（启动幂等补列；旧行默认 0 → 走文本查找兜底，**无需重新嵌入**；要精确偏移可跑一次「全量重嵌」）
  - 仅词法模式（`mode=lex`）同样返回块级信息：按同一套词法打分在块文本里挑最优块，**不额外发起 embedding 请求**
- 🖥 状态页新增「重排（rerank）」区块：可用性 / 模型 / 候选上限与超时 / 最近耗时 / 连续失败与冷却 / 降级原因；管理员可开关与改模型；试搜信息行显示本轮是否重排、候选块数与耗时；结果卡片新增「重排」分与「块」标记

### 接口
- `GET /api/search`：新增查询参数 `rerank=0|1`（不传=按全局配置），命中项新增块级字段，响应新增 `rerank` 汇总（`vec` 段语义不变）
- 新增 `GET /api/search/rerank`（状态）、`POST /api/search/rerank`（管理员，`{enabled?, model?}`）
- `GET /api/search/stats` 新增 `rerank` 段（取不到时返回不可用状态，不影响其它字段）
- `GET /api/kb/{kb}/search` 自动获得重排与块级字段（作用域、权限过滤不变）

### 部署（可选）
未部署重排服务时功能自动降级，检索行为与上一版**完全一致**。要启用：

```
llama-server --reranking -m bge-reranker-v2-m3.gguf --port 11435
```

然后在服务端设 `L_NOTEPAD_RERANK_URL=http://127.0.0.1:11435`（或只对单次请求带 `&rerank=1` 验证）。
### 注意
- 重排是**额外一次本地推理**：CPU 上 40 个候选块可能到几百毫秒，状态页「最近耗时」可作为调整 `L_NOTEPAD_RERANK_TOP_N` 的依据
- 首次调用即探测；失败结果缓存 60s（避免每次检索都等超时），改配置或页面切换开关会清掉探测与冷却状态、立即重试

---

## v3.1.0 (2026-09-17)

### 变更（索引源与构建时机）
- 📚 **知识库索引源改为「已上传的 depot 归档」**：不再索引各知识库的**本机工作区目录**（`knowledge_bases.workspace`）——服务端/无桌面环境（工作区目录不存在）此前会**整个跳过知识库源、完全搜不到**；现在改为递归列归档（`depot_map.list_tree()`，`GET /api/kb/{kb}/depot/list?recursive=1`）并按需下载内容建索引，个人笔记仍读本机文件
- ⏱ **构建时机优化**（原来只有"首次查询惰性建 + 请求内 5s TTL 全量扫描"）：
  - 🚀 **启动预热**：`search_index.warm_start()` 后台先全量比对笔记、再逐个同步知识库，首个查询不再承担建索引耗时；`stats.warm` 暴露进度
  - ⚡ **事件即时**：上传 / 提交 / 发布 / 改归档映射后 `notify_kb_change()` 让后台线程（`search_kb`）立刻同步对应知识库，上传请求不被下载拖慢
  - 🔁 **低频兜底**：后台 ticker 每 `KB_SCAN_TTL_S`（300s）列一次归档目录比对 `rev`，覆盖其它客户端上传的内容；请求路径**不再做任何 depot 网络访问**（原来 TTL 扫描挂在检索线程里）
  - 🧩 增量键：知识库按 `size + rev`（`search_docs.rev` / `vec_docs.rev` 新列，含迁移），只有 rev 变化才下载；取不到的版本（如 blob 缺失 410）记入 `_kb_skip` 不反复重试，`stats(deep=1)` 单列 `unavailable` 而不是报缺失
- 🖥 **知识库页面文件树 / 预览改走归档**：左侧树来自 `depot/list?recursive=1`（全部按"云端只读"处理，不再做本机↔云端逐篇内容比对，省掉 N 次往返）；预览读取顺序：客户端本地桥 → 托盘中转 → `/baidu` 直连 → **知识库后端 `/api/kb/{kb}/depot/file`**（服务端读归档兜底）；`?file=<rel>` 深链与搜索跳转仍可用；本机托盘模式（有授权目录时）保持不变
- 🧮 `stats()` 分来源明细：笔记行做磁盘校对，知识库行显示归档路径 / 文档数 / 最后索引时间 / 归档同步时间 / 同步错误；新增 `kb_scan_ttl_s` / `kb_pending` / `warm` 字段，索引状态页同步展示

### 新增功能
- ⬆⬆ **知识库页面「一键上传」按钮**（`/web/kb/{name}` 工具栏）：一次把工作区里**归档缺失或字节数变化**的文档批量提交到版本库归档——顺序逐个 `depot/submit`，按钮上显示 `上传中 n/总数`，结束汇总成功/失败（首个失败文件名），完成后自动重载归档与文件树；后端每次提交即时重建该知识库索引，上传完即可搜到
- 🔄 原「➕ 上传本地文件」改为同一套待上传判定（归档缺失 / 大小不同），本机托盘模式与服务端工作区模式都可用（服务端走 `GET /api/kb/{kb}/workspace` 列工作区文件）

### 修复
- 🔒 **`database is locked`**：后台索引线程原来"边下载/边扫描、边写库、最后才提交"，写事务跨越几十秒网络等待（预热实测 52s），把库锁死——知识库总览页的 `ensure_default_base` 等写请求等满 5s `busy_timeout` 后 500。现在：
  - 知识库同步改**两阶段**：先把要更新的内容全部下载到内存（不持有写事务），再逐篇写库并**每篇提交**；单次同步有配额（100 篇 / 32MB），未下完的本轮末尾继续（`_kb_more`）
  - 笔记全量扫描每 `_COMMIT_EVERY`（25）篇提交一次
  - 回归验证：预热/事件同步全程并发跑 2000+ 次写请求（`list_bases`），`database is locked` 0 次
- 🐛 顺带修掉同步里的**误删**隐患：归档里已是最新的条目未从"当前索引"集合剔除，会被当成"归档已删除"清掉（表现为同步后知识库索引全空）；现在以「归档里仍在的 rel 集合」判定删除，且下载配额提前中断时不做删除判定

### 注意
- 「取消发布」不会删归档文件，因此该文档仍在知识库索引与文件树中（索引 = 已上传内容）；彻底移除需在版本库页面删除归档路径
- 归档里 blob 缺失的文件（元数据在、内容取不到）不入索引，属正常跳过（`stats(deep=1)` 单列 `unavailable`）

---

## v3.0.0 (2026-09-16)

### 变更（破坏性修正）
- 🗂 **知识库归档映射修正**：原来把**知识库名当成 depot 库**（`/rez_pkg/xxx.md`），改为**库 `/notes` + 库内子路径**（`/notes/rez_pkg/xxx.md`）。新增 `depot_map.py`（不依赖 fastapi，可 headless 测试）：逻辑路径 = `{library}/{subpath}/{rel}`，`library` 默认 `/notes`、`subpath` 默认知识库名
- 🧩 **工作区映射**：每个知识库对应一个 depot 工作区 `kb-<名>`（P4 client 语义），绑定库与本地目录，并登记 `maps=[{depot_path: {base_path}, local_path: ""}]` → 本地 `xxx.md` ↔ `/notes/<kb>/xxx.md` 一一对应；`knowledge_bases` 新增 `depot_library` / `depot_subpath` / `depot_ws` 三列（含迁移）
- 🔀 **知识库页面不再直连 `/baidu/api/depot/*`**：列目录/读文件/提交/映射全部走后端接口
  - `GET/PUT /api/kb/{kb}/depot`（映射查询/修改，改完自动重建工作区映射）
  - `GET /api/kb/{kb}/depot/list?rel=`、`GET /api/kb/{kb}/depot/file?rel=&rev=`、`POST /api/kb/{kb}/depot/submit?rel=&description=`
  - 页面顶栏新增「归档：/notes/xxx」按钮，点击可改库内子路径
- 🔎 **知识库专用搜索接口** `GET /api/kb/{kb}/search?q=&mode=&limit=&offset=`：作用域在 SQL 内完成（`search_docs.kb_name`），前端不再"搜全部 kb 源再本地过滤"；`search_index.search()` 新增 `kb_name` 参数（词法/语义/权限过滤三处一致）
- 🚚 存量数据迁移：旧库 `/rez_pkg` 下的 8 个归档已 move 到 `/notes/rez_pkg/`（含子目录），旧库无文件残留

---

## v2.9.0 (2026-09-16)

### 新增功能
- 🎛 **embedding 模型可切换**：状态页「语义检索」新增模型下拉（`bge-m3` / `bge-base-zh-v1.5` / `bge-small-zh-v1.5` / `nomic-embed-text`，显示维度/上下文/体积/是否已安装），选择写入 `app_settings.embed_model`（优先级：环境变量 `L_NOTEPAD_EMBED_MODEL` > 页面设置 > 自动挑）
- ⬇ **切换未安装模型时提示下载，不主动下载**：`POST /api/search/model` 未安装只回 `need_download`（不下载、不切换），前端弹确认框；确认后才走 `POST /api/search/model/download`（Ollama `/api/pull` 流式进度，状态页显示下载百分比），下载完自动切换并提示重嵌
- 🧩 `GET /api/search/models`：模型目录 + 当前模型 + 下载进度；切换模型时清理旧模型的向量记录（`vec_docs` + `vec_chunks`）并清内存缓存，向量与当前模型不匹配时语义自动降级（`available.stale`）
- 📏 按模型自动调块大小：`chunk_size_for()` = min(900, ctx×0.8) 字符（bge-m3 8192→900；v1.5 系 512→**409**，避免尾部被截断），块重叠取块大小 1/8；`stats.chunk_size` 显示当前生效值
- 🔤 v1.5 系查询 instruction：`bge-*-zh-v1.5` 查询自动加「为这个句子生成表示以用于检索相关文章：」前缀（文档侧不加），`needs_query_instruction()` 判定；bge-m3 / nomic 不加
- 📊 阈值按模型标定（`vec_min()`）：实测本语料 bge-m3 负例 0.43-0.52 / 正例 0.65-0.70 → 阈值 **0.55**；bge-base-zh-v1.5 负例 0.23-0.29 / 正例 0.51-0.67 → 阈值 **0.35**（原来统一 0.45 会放进"今天天气不错"这类噪声）
- 🔍 语义不可用时回传原因：`vec.reason`（`向量需重新嵌入` / `模型未安装`），`sem` 模式语义零命中不再返回"词法 total + 空列表"的误导结果
- 🧪 双模型实测（真实 49 篇语料）：bge-base-zh-v1.5 与 bge-m3 的 top1 命中 4/5 一致、top3 集合一致；base-zh 模型小 5.8×、单块嵌入快约 4×（1437 块 18.2s vs 711 块 30-40s）、分数分离度更好 → **2C4G 服务器推荐 base-zh**；m3 在英文术语（`depot_blob`/`md5`）与超长文档上更强
- 🧠 **语义检索（向量）**：新增 `search_vec.py`——文档按段分块（900 字符、120 重叠）→ 本机 Ollama embedding（`/api/embed` 批量，回退 `/api/embeddings`）→ 归一化 float32 存 `vec_chunks`（`vec_docs` 记 mtime/size 做增量）；查询侧点积即余弦，每篇取最高分块分
- 🔀 混合检索：`GET /api/search?...&mode=hybrid|lex|sem`。`hybrid`（默认）= 词法结果 + 语义加分，**词法零命中时用语义兜底**；`sem` = 纯语义。命中项新增 `vec` 分数字段；`total` 计入语义补充的文档
- 🔎 摘要与高亮：摘要改为「短语优先 → 命中词块最密集窗口」定位；命中词以 `<mark>` 高亮（相邻/重叠区间合并，如「创建」+「建包」合成「创建包」），列表页服务端渲染（先转义再插标签，防 XSS），索引页试搜按 `matches` 前端高亮
- 🧮 状态页新增「语义检索（向量）」区块：可用性 / 模型 / 已嵌文档与块数 / 维度 / 进度条；`POST /api/search/embed_async[?force=1]`（管理员）后台增量或全量重嵌
- ⚙️ embedding 配置：`L_NOTEPAD_EMBED_URL`（默认 `http://127.0.0.1:11434`）、`L_NOTEPAD_EMBED_MODEL`（缺省自动挑 bge-m3 → bge-large-zh → nomic-embed-text）、`L_NOTEPAD_VEC_ENABLED=0` 可整体关闭

### 注意
- 中文语义质量取决于模型：`nomic-embed-text` 偏英文（实测「部署流程」误召回无关文档），已改为优先 `bge-m3`（多语言）。换模型后状态页点「全量重嵌」即可（`vec_docs.model` 与当前模型不一致会自动重嵌）

---

## v2.8.0 (2026-09-16)

### 新增功能
- 🔎 **搜索索引状态页** `/web/index`（左上角导航「搜索索引」）：索引总览（文档数 / 倒排行数 / 索引体积 / 待处理队列 / 扫描间隔 / 单文件上限）、分来源明细（根目录、文档数、最后索引时间、目录不存在告警）、**磁盘校对**（缺失 / 过期 / 多余 + FTS 完整性）、**后台重建进度条**、以及走同一套索引的「试搜」框
- 🧮 状态接口：`GET /api/search/stats[?deep=1]`（deep 做磁盘校对 + FTS `integrity-check`）、`POST /api/search/reindex_async`（管理员，后台重建，立即返回，进度见 stats）
- 🔍 知识库页（`/web/kb/{name}`）顶栏恢复**搜索栏**：300ms 去抖后检索**本知识库**工作区内容（走倒排索引），命中结果替换左侧文件树（带覆盖率、tooltip 显示命中摘要），清空即回到文件树；知识库总览页顶栏也给全局搜索入口
- 🩹 引号查询兜底：`"精确短语"` 无结果时自动回退为整串模糊匹配，响应带 `fallback`，列表页提示「未找到精确短语，已显示模糊结果」

### 内部改动
- 相关性打分升级（不再只看短语+覆盖率）：`score = 3×短语命中 + 2×覆盖率 + 1×词频 + 1×近邻度 − 1.5×bm25`。词频按词块出现次数加权（封顶 5，避免长文堆词）；近邻度按各词块首现位置的跨度衰减（200 字符尺度，跨度越大越低，短语命中直接记满分）；bm25 权重从 0.2 提到 1.5（标题列权重 6 / 正文 1 真正参与排序）。API 命中项新增 `tf` / `proximity` / `bm25` 字段，索引页试搜一并展示
- 后台重建与查询互不阻塞：重建期间 `refresh()` 直接跳过（读旧索引，搜索照常可用）
- `stats()` 的 FTS 完整性检查后立即收尾事务，避免占住写锁挡住后台重建

---

## v2.7.1 (2026-09-16)

### 问题修复
- 🐛 修复公网部署机（`Lugwit_deploy=1`）网页端**所有人都登录不上**（`/note/login` 恒定 401「用户名或密码错误」）：`server_config.py` 的服务端默认 host 曾按机器类型切到公网入口（域名 `https://lugwit.duckdns.org` 或裸 IP `https://121.196.144.88`），而服务端调认证服务是**同机调用**——域名回环在部分网络下不通，裸 IP 入口是自签证书，`urllib` 默认校验证书直接 `SSL CERTIFICATE_VERIFY_FAILED`。现固定走本机 nginx 回环入口 `http://127.0.0.1:8080`（与开发机一致，`/api/v1/auth` → 1027）。客户端侧的默认地址在 `l_notepad_client/server_config.py`，不受影响
- 🐛 认证服务不可用不再伪装成密码错误：`auth.login` 区分「认证服务不可用」（抛 `AuthUnavailable` 并记日志）与「用户名或密码错误」，登录接口回 503「认证服务不可用，请稍后重试或联系管理员」，不再让用户误以为是密码问题

---

## v2.7.0 (2026-09-16)

### 新增功能
- 🔍 搜索路由 `GET /api/search?q=&limit=&offset=&sources=`：FTS5 倒排索引检索（`search_fts` + `search_docs` 表，见 `search_index.py`），只查索引不读文档，返回命中摘要 / 覆盖率 / `open_url` / 总命中数（分页）；`sources=note,kb` 可过滤来源
- 📚 索引源含**知识库工作区**：除个人笔记（`source=note`）外，各知识库 `workspace` 目录下的 `.md/.txt/.rst/.log` 也入索引（`source=kb`，登录可见）；列表页命中卡片带「📚 知识库」标签并直达 `?file=<rel>` 定位
- 🎯 宽召回 + 相关性排序：中文按 bigram **OR** 召回（不再"精确无果才兜底"），排序按「命中短语 > bigram 覆盖率 > bm25 > 时间」；`"引号"` 包住的片段转 FTS5 短语要求精确命中（搜 `创建包` 能命中「创建 Rez 包」的文档，`"创建包"` 则只命中连写）
- ⚡ 索引增量维护：本服务内增删改经 `file_store` 变更通知即时标脏、下次查询补索引；外部改动（桌面端落盘、托盘写入）由 TTL（5s）全量比对 `mtime/size` 兜底，内容未变的文件不重读
- 🈶 中文检索：写入前按二元切分（bigram）再入倒排表（unicode61 不切汉字，整段汉字会变成一个 token）
- 🔧 `POST /api/search/reindex`（管理员）：清空重建全部索引（含知识库工作区）
- 🚀 列表页 `/web?q=` 改为走倒排索引（原实现每次请求逐篇读全文、单文件 2MB 上限），结果按相关度排序并展示命中摘要
- 💻 知识库工作区支持**本机模式**：后端在远程机时 `/web/kb/{name}` 的工作区可改读**浏览器所在机器**的目录——页面探测本机托盘 `http://127.0.0.1:19527/health`（有 `kb_ws_list` 即启用），列/读/写/打开目录改走托盘白名单动作（`kb_ws_*`），首次需点「📂 选择本机目录」由用户授权（root 写入 `~/.Lugwit/l_tray/kb_ws_roots.json`）；托盘不在线时自动回退服务端工作区
- 🔓 CSP `connect-src` 放行 `http://127.0.0.1:19527`（本机托盘 ExecServer），否则浏览器会拦掉本机模式请求

### 内部改动
- `file_store.set_note_change_hook` 单槽改为多订阅者（`add_note_change_hook` / `remove_note_change_hook`），`cloud_sync` 改用 add/remove（原先 `stop()` 会卸载搜索索引订阅）

---

## v2.6.0 (2026-09-02)

### 新增功能
- 📚 知识库：把当前笔记**快照发布**为知识库文章（`POST /api/kb/publish`，编辑页顶栏「📚 发布到知识库」按钮，选择预设层级），发布后与源笔记解耦（改名/改内容不影响已发布快照），再次发布覆盖旧快照
- 🗂 预设层级：知识库内预置可多级的目录树（`knowledge_categories` 表，`/web/kb` 侧栏可视化新增/重命名/删除，路径前缀随父级自动迁移）
- 📄 知识库网页 `/web/kb`：侧栏层级树筛选 + 「全部文章」，文章卡片列表，点击打开 Markdown 渲染阅读（marked + DOMPurify 复用编辑器预览管线），可「下架本文」取消发布
- 🧭 日志页新增「📚 知识库」导航按钮：日志查看器顶栏一键跳到知识库页（`{{ web_base }}/kb`）

### 问题修复
- 🐛 源笔记被删除时同步清理知识库文章（`web_delete_post` / `delete_note` 调用 `knowledge.delete_note_cleanup`），避免「已撤销」快照残留

### 安全
- 🔒 发布/查看知识库均要求登录且具备该笔记访问权限（`note_access.can_access` + `admin` 全可见）

---

## v2.5.0 (2026-09-02)

### 新增功能
- 👁 管理员全可见：管理员（role 1/2）列表/详情页可看到**全部已注册笔记**（只读，写/删/共享仍按归属权限）；普通用户视野不变（`can_access`/`list_accessible` 增加 `admin` 参数）
- 🔃 列表页排序：侧栏「笔记列表」标题右侧 名称/时间 按钮，点击切换维度、再点切换升降序（`localeCompare("zh-Hans-CN")` 中文拼音序），导航列表与卡片网格同步排序

### 问题修复
- 🐛 修复列表页卡片/导航项被相对时间脚本清空：外层 `<a data-ts>`（排序用）被 `[data-ts]` 全局选择器 `textContent=` 抹掉全部子节点，现只改内层 `.time`/`.card-time` 元素

---

## v2.4.0 (2026-09-01)

### 新增功能
- 🧠 `.md` 文件即脑图超集：```` ```mindmap ```` 代码块在预览区直接变为**可交互编辑器**——拖拽移动子树、双击改名、工具栏/Tab/Enter 增节点、Delete 删除；修改实时 splice 回 Markdown 源文（其余文本原样保留，多个脑图块偏移自动平移），Ctrl+S 保存即持久化
- 🔀 兼容两种脑图语法：`#` 标题层级与 `-` 缩进列表可混用；编辑后统一序列化为列表格式（语义等价）
- 🔒 无编辑权限（只读共享）时脑图自动退化为静态渲染
- 🗂 `.mmd` 纯脑图格式保留（整篇即脑图，全屏编辑器）

### 问题修复
- 🐛 修复 Windows 表单保存后空行翻倍：textarea CRLF + `write_text` 默认换行翻译叠加成 `\r\r\n`（`file_store` 写入 `newline="\n"` + 表单入口归一化）
- 🐛 修复脑图工具栏 `container.focus()` 抢走改名输入框焦点导致输入框闪没

---

## v2.3.0 (2026-09-01)

### 新增功能
- 📁 笔记分组：按目录树组织笔记，新建时选分组/输入新分组，编辑页可移动分组；列表页侧栏分组树过滤（`GET /api/groups`、`POST /api/notes/{path}/move`）
- 🏷 笔记标签：SQLite `note_tags` 表存储，编辑页 chips 式标签编辑器（回车添加/退格删除/已有标签自动补全），随表单保存同步；列表页标签云 + 卡片标签展示；改名/移动/删除时标签自动跟随（`note_tags` 系列 API、`GET /api/tags`）
- 👤 创建者筛选：列表页按创建者过滤（`GET /api/creators`），卡片/侧栏显示归属
- 🧠 笔记内嵌脑图：Markdown 中 ```` ```mindmap ```` 代码块实时渲染为 SVG 脑图（零依赖自研渲染器 `static/mm.js`，标题层级 + 缩进列表混合语法，markmap 兼容语义：列表挂最近标题之下）；编辑器工具栏「插入脑图」按钮一键插入骨架

### 改进优化
- 🎨 列表页侧栏重构：分组/标签/创建者三区 + 笔记列表统一滚动；搜索、分组、标签、创建者四维组合过滤（前端实时）
- 🔒 标签与共享数据均按可见性过滤（标签云/创建者列表只含当前用户可见笔记）

---

## v2.2.0 (2026-09-01)

### 安全修复
- 🔒 服务器日志 API（`/api/logs*`）全部收紧为管理员专属：原先任意登录用户可读取/篡改/删除服务器日志
- 🔒 登录 Cookie 改为服务端 `Set-Cookie` 下发（`HttpOnly` + `SameSite=lax`），防 XSS 窃取 token；登出改为服务端清 cookie
- 🔒 修复共享列表存储型 XSS：共享用户名改用 DOM `textContent` 渲染，不再拼 `innerHTML`
- 🔒 后端默认监听地址 `0.0.0.0` → `127.0.0.1`（对外暴露需显式 `--host`，生产走 nginx `/note` 反代）

### 问题修复
- 🐛 修复新建笔记后连续 Ctrl+S 重复创建：旧正则只匹配数字 ID，现按 303 重定向的最终 URL 更新地址栏与表单 action
- 🐛 修复笔记改名后注册表/共享关系不迁移：改名后笔记从列表消失、共享关系悬空，现随改名同步迁移
- 🐛 修复 `POST /web/{path}/delete` 被 `POST /web/{path}` 路由吞掉导致删除按钮 403（路由注册顺序）

### 新增功能
- ✨ 接入本机 Auth Service（`http://localhost:8080/auth`，文档 `/auth/docs`）
- ✨ 新增服务日志查看器 `/web/logs`（管理员）：日志级别着色（INFO/WARN/ERROR/CRITICAL）、行号、行数上限、关键字过滤、下载、SSE 实时跟踪追加（Dozzle 式 tail -f）
- ✨ `/web?q=` 搜索升级为服务端全文检索（单文件 2MB 上限），不再只匹配 8KB 摘要
- ✨ 编辑页增加未保存离开提醒（beforeunload）

### 改进优化
- 🚀 笔记列表 TTL 缓存（3s + 写入失效），避免每请求全目录扫描
- 🚀 日志读取默认只读文件尾部（`?tail=` 字节，上限 4MB），大日志不再整读进内存
- 🚀 SQLite 开启 WAL + busy_timeout；改为每请求独立连接，消除共享连接的线程竞态
- 🚀 启用 GZip 压缩（≥1KB）
- ♻️ 后端按领域拆分路由：`routers/{notes,logs,admin,accounts,web}.py` + `deps.py` 依赖注入（FastAPI 官方模板模式）
- ♻️ 模板抽 `base.html` + `static/app.css` + `static/app.js` 公共层，去除 5 个模板重复的样式与登出脚本
- ♻️ 移除 `db.py` 中已废弃的 `notes` 表 CRUD 死代码

---

## v2.1.0 (2026-06-21)

### 新增功能
- ✨ 文件夹收藏页面支持网址收藏功能
- ✨ 添加独立的「🌐 网址收藏」标签页
- ✨ 文件夹和命令 item 添加不同类型图标区分（文件夹图标/命令图标）
- ✨ 支持鼠标拖拽排序收藏项，自动保存到 JSON

### 改进优化
- 🎨 右键菜单优化：添加命令/网址菜单移至标签页按钮右键
- 🎨 剪贴板历史列表间距优化，item 高度降低至 16px，显示更紧凑
- 🔧 重命名对话框改进：支持名称和值联动编辑（带锁复选框）
- 🔧 类型安全增强：使用 TypedDict 定义收藏项结构

### 问题修复
- 🐛 修复剪贴板列表 item 间距过大的问题（使用 ItemDelegate 强制高度）
- 🐛 修复 l_scheduler 设置窗口 QCheckBox 已删除错误（动态加载模块缓存问题）

### 技术债务
- ♻️ 重构 RenameItemDialog 为独立对话框类
- ♻️ 优化 UI 文件样式定义，统一紧凑布局

---

## v2.0.0 (2026-06-21)

### 新增功能
- ✨ 集成 AI 对话功能（支持 SiliconFlow 和智谱 API）
- ✨ 添加文件夹收藏夹功能，支持快捷键快速访问
- ✨ 实现本地 API 服务器模式，支持外部程序调用
- ✨ 添加代码编辑器（语法高亮、缩进显示）
- ✨ 支持 Web UI 访问模式

### 改进优化
- 🚀 优化窗口重启流程，解决进程残留问题
- 🎨 改进无边框窗口样式，支持圆角和阴影
- 📱 响应式标题栏布局，窗口变窄时自动切换为垂直排列
- 💾 改进数据存储方式，从 SQLite 迁移到文件存储

### 问题修复
- 🐛 修复最小化恢复后内容区空白的问题
- 🐛 修复 Windows 平台任务栏图标不显示的问题
- 🐛 修复多显示器环境下窗口位置偏移的问题
- 🐛 修复重启时 cmd.exe 残留进程的问题

### 技术债务
- ♻️ 重构窗口架构，使用 L_FramelessMainWindow 作为外壳
- ♻️ 改进线程安全机制，使用装饰器模式替代 __getattribute__ 劫持
- ♻️ 优化单实例锁机制，防止多开

---

## v1.0.0 (初始版本)

### 核心功能
- 📝 基础笔记本功能（创建、编辑、删除笔记）
- 📁 文件夹管理（创建、重命名、删除）
- 🔍 全文搜索功能
- 🎯 标签系统
- 📋 剪贴板集成

### 技术栈
- Python 3.10+
- PySide6 (Qt6)
- FastAPI (后端服务)
- SQLite (数据存储)
