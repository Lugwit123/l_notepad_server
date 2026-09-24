# -*- coding: utf-8 -*-
"""笔记 / 知识库工作区全文搜索索引（SQLite FTS5 倒排表）

目标：像搜索引擎一样「查索引」，而不是每次请求把每篇文档读一遍。

索引源（两类，都不依赖「本机工作区目录」）：
  - 个人笔记：`notepad_list` 下所有文件（source='note'，权限按归属/共享判定）
  - 知识库归档：各知识库**已上传到 depot 服务**的版本（source='kb'，登录可见）；
    本机工作区目录只用于上传 / 编辑，不参与索引

召回与排序：
  - 中文按二元切分（bigram）入索引（unicode61 不切汉字，整段汉字会变成一个 token）；
    英文/数字原样小写；写入前自行切分，读取用同一套规则切查询串。
  - 查询：多字中文段的各 bigram 之间 **OR 召回**（宽召回），空格分隔的多段之间 AND；
    `"引号"` 段转成 FTS5 短语（bigram 序列相邻）→ 精确匹配。
  - 排序：命中短语 > 覆盖率 > 词频/近邻度 > bm25（标题权重 6 / 正文 1），
    即「创建包」连写的文档排在只命中「创建」的文档前面，接近搜索引擎行为。

构建时机：
  - 笔记：服务内增删改经 file_store 变更通知即时标脏，下次查询补索引；桌面端直接落盘 /
    托盘写入等外部改动由 TTL 全量比对（只 stat 比 mtime/size，内容没变不读文件）兜底。
  - 知识库：上传 / 提交 / 发布 / 取消发布后**事件即时**刷新对应知识库（后台线程，不占用
    请求）；后台 ticker 每 KB_SCAN_TTL_S 秒列归档目录比对 rev 兜底；服务启动预热一次。
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import file_store

# 单文件索引上限（超大文件只索引前 2MB，避免拖慢建索引）
MAX_INDEX_BYTES = 2 * 1024 * 1024
# 外部改动（不经过本服务钩子）允许的最长陈旧时间（全量比对只 stat，不读文件，开销很小）
SCAN_TTL_S = 5.0
# 知识库归档（depot）兜底扫描间隔：事件即时刷新之外的保险，列目录比对 rev 才下载内容
KB_SCAN_TTL_S = 300.0
# 事件触发后的防抖窗口：连续多次提交合并成一次同步
KB_DEBOUNCE_S = 1.0
# 「重建历史」保留条数（search_history 表，状态页展示）
HISTORY_KEEP = 200
# 全量扫描/同步期间每处理多少篇提交一次（及时释放 SQLite 写锁，避免别的请求 database is locked）
_COMMIT_EVERY = 25
# 单次知识库同步的下载配额（html 文本，避免一轮把整库内容都读进内存）
_KB_FETCH_MAX_DOCS = 100
_KB_FETCH_BYTES = 32 * 1024 * 1024
# 单次检索返回条数上限
MAX_LIMIT = 500
# 重排取回的候选倍数（先按 bm25 取 offset+limit 的 K 倍，再按相关性重排）
_CANDIDATE_FACTOR = 6

# 相关性打分权重（bm25 为 FTS 原始值，越负越相关，取负后越大越好）
_W_PHRASE = 3.0    # 引号短语精确命中
_W_COVERAGE = 2.0  # 查询词块覆盖率
_W_TF = 1.0        # 词频（按词块出现次数，封顶）
_W_PROX = 1.0      # 近邻度（词块首现位置越集中越高）
_W_BM25 = 1.5      # 词频×IDF×长度归一×列权重（标题 6 / 正文 1）
_TF_CAP = 5        # 单个词块词频封顶，避免长文堆词刷分
_PROX_SPAN = 200.0  # 近邻度尺度：首现位置跨度达 200 字符时该分项减半

# 语义（向量）检索融合参数
_W_VEC = 1.2       # 语义相似度权重（0~1 之间，乘该系数后并入总分）
_VEC_MIN = 0.45    # 语义相似度下限，低于此值视为不相关，不参与融合（bge-m3 实测噪声 <0.5）
_VEC_REL = 0.15    # 相对窗口：只保留与最高分相差不超过该值的语义命中

# 知识库工作区可索引的文本类扩展名（与 routers/kb.py 的浏览白名单一致）
WORKSPACE_EXTS = {".md", ".markdown", ".txt", ".rst", ".log"}

# 代码库索引（source="code"，见 code_roots / _scan_code）：可索引的代码/配置扩展名
CODE_EXTS = WORKSPACE_EXTS | {
    ".py", ".pyi", ".ui", ".qss", ".bat", ".cmd", ".ps1", ".sh",
    ".toml", ".yaml", ".yml", ".ini", ".cfg", ".json",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".rs", ".go",
}
# 代码库扫描跳过的目录名（依赖 / 构建产物 / 缓存 / VCS）
CODE_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".idea", ".vscode", "dist", "build", "target", ".mypy_cache", ".pytest_cache",
    ".tox", ".next", ".cache", ".gradle", "site-packages",
}
# 代码库根目录（换行/分号分隔的绝对路径）持久化在 app_settings
SETTING_CODE_ROOTS = "code_roots"

# 选库路由（route）：关键词块上限 + 打分权重（见 route()）
_ROUTE_MAX_TERMS = 40    # 关键词块上限，防超长需求拖慢
_ROUTE_W_DOCS = 0.5      # 库内命中量（log1p，弱权重，防大库霸榜）
_ROUTE_W_META = 2.5      # 库名/标题/描述命中（元数据信号）
_ROUTE_W_VEC = 1.5       # 库摘要语义相似度（depth>=2）
_ROUTE_W_BM25 = 1.5      # 库内最优 bm25（自带宽 IDF，压低高频泛词）
_ROUTE_BM25_SCALE = 20.0  # bm25 → (-1,1) 的 tanh 尺度（避免原始分主导）
# 结果缓存：Agent 常重发同一需求；短 TTL 直接命中（改索引后最多陈旧一会儿）
_ROUTE_CACHE_TTL = 10.0
_ROUTE_CACHE_MAX = 200
# 选库路由剔除的低信息单字：「的」「了」这类 bigram 会带来大量噪声召回
_ROUTE_STOP_CHARS = set(
    "的了是在和与或这那之其也就都还而并且把被为对从到很会能可要需请吧吗呢啊呀哦嗯"
    "我你他她它们个来去做用以于上下中里外前后时"
)

_HAN = r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
_SEG_RE = re.compile(f'("{_HAN}+"|{_HAN}+|[0-9A-Za-z]+)')

# 可见性过滤：笔记按归属 / 共享 / 管理员全可见；知识库工作区对登录用户可见
_PERM_SQL = """
  AND (
    (
      search_docs.source = 'note'
      AND (
        :admin = 1
        OR search_docs.rel IN (SELECT note_path FROM note_registry WHERE owner_username = :user)
        OR search_docs.rel IN (
          SELECT note_path FROM note_shares WHERE shared_with = :user OR shared_with = '*'
        )
      )
    )
    OR (
      search_docs.source = 'kb'
      AND EXISTS (SELECT 1 FROM knowledge_bases kb WHERE kb.name = search_docs.kb_name)
    )
    OR (
      search_docs.source = 'code'
    )
  )
"""

_hooks_installed = False
_lock = threading.Lock()
_refresh_lock = threading.Lock()
_pending: dict[str, str] = {}      # 笔记相对路径 -> 'upsert' | 'delete'
_last_scan: dict[str, float] = {}  # notes_root(str) -> monotonic

# 知识库（depot 归档）同步：事件标脏 + 后台线程（防抖合并），请求路径一律不碰网络
_kb_pending: set[str] = set()      # 待同步的知识库名
_kb_errors: dict[str, str] = {}    # 知识库名 -> 最近一次同步错误（状态页展示）
_kb_last_sync: dict[str, float] = {}   # 知识库名 -> monotonic
_kb_skip: dict[tuple[str, str], int] = {}   # (知识库, rel) -> 取不到内容的 rev（避免反复重试）
_kb_more: set[str] = set()             # 因下载配额提前结束的知识库（本轮末尾继续同步）
_kb_wake = threading.Event()
_kb_started = False

# 启动预热状态（首个查询不再承担全量建索引的耗时）
_warm_state: dict[str, Any] = {
    "running": False,
    "phase": "",        # notes（笔记全量比对）/ kb（逐个知识库同步）
    "updated": 0,
    "started_at": "",
    "finished_at": "",
    "error": "",
}

# 后台重建索引的任务状态（供「搜索索引」状态页轮询显示进度）
_reindex_state: dict[str, Any] = {
    "running": False,
    "phase": "",        # counting（统计文件数）/ indexing（建索引）
    "total": 0,        # 预计要索引的文件总数（磁盘扫描得出）
    "done": 0,         # 已处理文件数
    "updated": 0,      # 实际写入/删除的索引行数
    "started_at": "",
    "finished_at": "",
    "error": "",
}


def _kb_key(kb_name: str, rel: str) -> str:
    return f"kb:{kb_name}:{rel}"


# ── 分词 / 查询解析 ─────────────────────────────────────


def _tokens(text: str) -> Iterable[str]:
    """中文连续段 → 二元组（单字保留原样）；英文/数字串 → 小写单词。"""
    for m in _SEG_RE.finditer(text or ""):
        seg = m.group(0)
        if seg[0].isascii():
            yield seg.lower()
        elif len(seg) == 1:
            yield seg
        else:
            for i in range(len(seg) - 1):
                yield seg[i : i + 2]


def index_text(text: str) -> str:
    """建索引用文本：token 以空格连接。"""
    return " ".join(_tokens(text))


def parse_query(query: str, *, loose: bool = False) -> list[dict[str, str]]:
    """查询串 → 匹配单元列表（`loose=True` 时忽略引号，把引号内内容按普通大段处理）。

    单元类型：
      word   —— 英文/数字单词（整词匹配）
      char   —— 单个汉字（前缀匹配）
      bigram —— 多字中文段切成的一枚 bigram（OR 召回 + 覆盖率打分）
      phrase —— `"引号"` 里的中文短语（相邻 bigram 序列，精确匹配，排序最优先）
    """
    units: list[dict[str, str]] = []
    for m in _SEG_RE.finditer(query or ""):
        seg = m.group(0)
        quoted = seg.startswith('"')
        if quoted and not loose:
            inner = seg.strip('"')
            if len(inner) == 1:
                units.append({"type": "char", "text": inner})
            else:
                units.append({"type": "phrase", "text": inner})
            continue
        if quoted:
            seg = seg.strip('"')
        if not seg:
            continue
        if seg[0].isascii():
            units.append({"type": "word", "text": seg.lower()})
        elif len(seg) == 1:
            units.append({"type": "char", "text": seg})
        else:
            units.extend({"type": "bigram", "text": seg[i : i + 2]} for i in range(len(seg) - 1))
    return units


def _cluster_units(units: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """把同一原始段切出的连续 bigram 聚成一组（组内 OR，组间 AND）。"""
    groups: list[list[dict[str, str]]] = []
    for u in units:
        if groups and u["type"] == "bigram" and groups[-1][-1]["type"] == "bigram":
            groups[-1].append(u)
        else:
            groups.append([u])
    return groups


def build_match_expr(units: list[dict[str, str]]) -> str:
    """单元分组后拼 FTS5 表达式：组内 OR（宽召回），组间 AND，短语/单词自成一组。"""
    def term(u: dict[str, str]) -> str:
        t = u["text"]
        if u["type"] == "char":
            return f'"{t}" *'
        if u["type"] == "phrase":
            phrase = " ".join(t[i : i + 2] for i in range(len(t) - 1))
            return f'"{phrase}"'
        return f'"{t}"'

    parts: list[str] = []
    for g in _cluster_units(units):
        terms = [term(u) for u in g]
        parts.append("(" + " OR ".join(terms) + ")" if len(terms) > 1 else terms[0])
    return " AND ".join(parts)


def _route_stop_bigram(t: str) -> bool:
    """含任一低信息单字的 bigram 视为噪声（如「需要」「一个」）。"""
    return any(ch in _ROUTE_STOP_CHARS for ch in t)


def route_terms(query: str, *, limit: int = _ROUTE_MAX_TERMS) -> list[str]:
    """选库路由用的关键词块：中文二元 OR（剔除低信息 bigram）+ 英文整词；去重保序。

    与 `parse_query` 的关键区别：**不做段间 AND**——复杂长需求按段间 AND 会直接
    零命中（这正是 `/api/search` 对长句失效的原因）；这里全部 OR 召回，再按覆盖打分。
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in _SEG_RE.finditer(query or ""):
        seg = m.group(0).strip('"')
        if not seg:
            continue
        if seg[0].isascii():
            toks = [seg.lower()]
        elif len(seg) == 1:
            continue    # 单字前缀召回噪声大，选库不用
        else:
            toks = [seg[i : i + 2] for i in range(len(seg) - 1)]
            toks = [t for t in toks if not _route_stop_bigram(t)]
        for t in toks:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
                if len(out) >= limit:
                    return out
    return out


def route_match_expr(terms: list[str]) -> str:
    """选库路由的 FTS5 表达式：全部关键词块 **OR** 召回（无段间 AND）。"""
    return " OR ".join(f'"{t}"' for t in terms if t)


def highlight(text: str, matches: list[str], limit: int = 200) -> str:
    """把命中词包成 <mark>：先转义，再按位置区间标注，相邻/重叠区间合并。

    重叠合并让「创建」+「建包」这类相邻 bigram 合成一段高亮（创建包），也不会产生嵌套标签。
    """
    from html import escape

    raw = (text or "").replace("\x00", "").replace("\x01", "")
    out = escape(raw)
    low = out.lower()
    spans: list[list[int]] = []
    for term in sorted(matches or [], key=len, reverse=True):
        t = escape(term or "").lower()
        if not t:
            continue
        start = 0
        while len(spans) < limit:
            p = low.find(t, start)
            if p < 0:
                break
            end = p + len(t)
            spans.append([p, end])
            start = p + 1
    if not spans:
        return out
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    parts: list[str] = []
    last = 0
    for s, e in merged:
        parts.extend([out[last:s], "<mark>", out[s:e], "</mark>"])
        last = e
    parts.append(out[last:])
    return "".join(parts)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _anchor(text_low: str, units: list[dict[str, str]], width: int = 180) -> tuple[int, list[str]]:
    """摘要定位 + 高亮词。

    1) 有引号短语且命中 → 用短语位置（精确、最相关）；
    2) 否则取「命中词块最密集」的窗口（同宽内出现的不同词块最多，并列取最早），
       这样「创建…建包」挨着写的位置会被优先展示，而不是文档里最早出现的那个词。
    返回 (位置, 高亮词列表)；都没命中返回 (-1, [])。
    """
    phrases = [u["text"] for u in units if u["type"] == "phrase"]
    for t in phrases:
        pos = text_low.find(t.lower())
        if pos >= 0:
            return pos, [t]

    terms = sorted({u["text"] for u in units if u["type"] != "phrase"}, key=len, reverse=True)
    present = [t for t in terms if t.lower() in text_low]
    if not present:
        return -1, []
    if len(present) == 1:
        return text_low.find(present[0].lower()), present

    points: list[tuple[int, str]] = []
    for t in present:
        tl, start, seen = t.lower(), 0, 0
        while seen < 25:  # 单词块取样上限，避免长文里海量命中拖慢
            p = text_low.find(tl, start)
            if p < 0:
                break
            points.append((p, tl))
            start, seen = p + 1, seen + 1
    points.sort()

    best_pos, best_n = points[0][0], 0
    for i in range(len(points)):
        j, kinds = i, set()
        while j < len(points) and points[j][0] - points[i][0] <= width:
            kinds.add(points[j][1])
            j += 1
        if len(kinds) > best_n:
            best_n, best_pos = len(kinds), points[i][0]
        if best_n == len(present):
            break  # 已经命中全部词块，不必再扫
    return best_pos, present


def _chunk_pos(text: str, units: list[dict[str, str]], chunk_text: str, chunk_offset: int) -> tuple[int, list[str]]:
    """用命中块定位摘要：块偏移有效则用偏移，否则按块首行/前缀在正文里查找。

    返回 (位置, 高亮词)；块内没有查询词时高亮词为空（语义命中本来就没有词法依据）。
    """
    chunk = (chunk_text or "").strip()
    if not chunk:
        return -1, []
    pos = -1
    if 0 <= int(chunk_offset) < len(text):
        head = chunk[:16]
        if text.startswith(head, int(chunk_offset)):
            pos = int(chunk_offset)
    if pos < 0:
        # 长前缀最精确 → 首行（段间空行被合并时仍可用）→ 兜底前缀
        first = chunk.split("\n", 1)[0].strip()
        for probe in (chunk[:60], first, chunk[:80]):
            if len(probe) < 4:
                continue
            pos = text.find(probe)
            if pos >= 0:
                break
    if pos < 0:
        return -1, []

    low = chunk.lower()
    phrases = [u["text"] for u in units if u["type"] == "phrase" and u["text"].lower() in low]
    if phrases:
        return pos, phrases[:1]
    present = [
        t for t in sorted({u["text"] for u in units if u["type"] != "phrase"}, key=len, reverse=True)
        if t.lower() in low
    ]
    return pos, present


def _snippet(
    body: str,
    units: list[dict[str, str]],
    width: int = 180,
    lead: int = 60,
    chunk_text: str = "",
    chunk_offset: int = 0,
) -> tuple[str, list[str]]:
    """命中位置附近的摘要 + 高亮词列表。

    定位顺序：命中块（偏移 → 文本查找）→ 短语优先 → 命中词块最密集窗口。
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "", []
    pos, matches = _chunk_pos(text, units, chunk_text, chunk_offset)
    if pos < 0:
        pos, matches = _anchor(text.lower(), units)
    if pos < 0:
        head = text[:width].replace("\n", " ")
        return head + ("…" if len(text) > width else ""), []
    start = max(0, pos - lead)
    end = min(len(text), start + width)
    snip = ("…" if start > 0 else "") + text[start:end].replace("\n", " ") + ("…" if end < len(text) else "")
    return snip, matches


# ── 命中打分（覆盖率 + 短语）─────────────────────────────


def _score(body_low: str, units: list[dict[str, str]], bm25_rank: float) -> dict[str, Any]:
    """相关性元数据：短语命中 / 覆盖率 / 词频 / 近邻度 / bm25 → 加权总分。

    权重见 `_W_*`：短语 3、覆盖率 2、词频与近邻各 1、bm25 1.5（bm25 取负后加分）。
    词频封顶（`_TF_CAP`），近邻度按命中词块首现位置的跨度衰减（`_PROX_SPAN`）。
    """
    phrases = 0
    covered = 0
    total = 0
    tf_sum = 0
    positions: list[int] = []
    for u in units:
        if u["type"] == "char":
            continue  # 单字信息量太低，不参与覆盖率/词频
        text = u["text"].lower()
        total += 1
        hits = body_low.count(text)
        if not hits:
            continue
        covered += 1
        tf_sum += min(hits, _TF_CAP)
        positions.append(body_low.find(text))
        if u["type"] == "phrase":
            phrases += 1
    coverage = (covered / total) if total else 1.0
    tf_score = (tf_sum / (_TF_CAP * total)) if total else 1.0
    if len(positions) >= 2:
        spread = max(positions) - min(positions)
        proximity = 1.0 / (1.0 + spread / _PROX_SPAN)
    else:
        proximity = 0.0
    if phrases:
        proximity = 1.0  # 短语命中本身就是"相邻"的最强证据
    score = (
        _W_PHRASE * phrases
        + _W_COVERAGE * coverage
        + _W_TF * tf_score
        + _W_PROX * proximity
        - _W_BM25 * float(bm25_rank)
    )
    return {
        "phrase_hits": phrases,
        "coverage": round(coverage, 3),
        "tf": round(tf_score, 3),
        "proximity": round(proximity, 3),
        "score": round(score, 4),
    }


# ── 索引维护 ────────────────────────────────────────────


def install() -> None:
    """注册 file_store 变更通知（幂等；应用启动时调用一次）。"""
    global _hooks_installed
    if not _hooks_installed:
        file_store.add_note_change_hook(_on_note_change)
        _hooks_installed = True


def _on_note_change(action: str, rel_path: str) -> None:
    with _lock:
        _pending[rel_path] = action


def _rowid(conn, key: str) -> int | None:
    row = conn.execute("SELECT rowid FROM search_docs WHERE note_path = ?", (key,)).fetchone()
    return int(row["rowid"]) if row else None


def _upsert(
    conn,
    key: str,
    body: str,
    size: int,
    mtime: float,
    *,
    source: str = "note",
    kb_name: str = "",
    rel: str = "",
    rev: int = 0,
) -> None:
    """写入/覆盖一篇文档（rowid 不变，FTS 行先删后插）。"""
    rel = rel or key
    now = datetime.now().isoformat(timespec="seconds")
    rowid = _rowid(conn, key)
    if rowid is None:
        cur = conn.execute(
            "INSERT INTO search_docs(note_path, source, kb_name, rel, title, body, size, mtime, rev, indexed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (key, source, kb_name, rel, rel, body, size, mtime, int(rev), now),
        )
        rowid = int(cur.lastrowid)
    else:
        conn.execute("DELETE FROM search_fts WHERE rowid = ?", (rowid,))
        conn.execute(
            "UPDATE search_docs SET source = ?, kb_name = ?, rel = ?, title = ?, body = ?,"
            " size = ?, mtime = ?, rev = ?, indexed_at = ? WHERE rowid = ?",
            (source, kb_name, rel, rel, body, size, mtime, int(rev), now, rowid),
        )
    conn.execute(
        "INSERT INTO search_fts(rowid, title, body, note_path) VALUES(?,?,?,?)",
        (rowid, index_text(rel), index_text(body), key),
    )


def _drop(conn, key: str) -> None:
    rowid = _rowid(conn, key)
    if rowid is None:
        return
    conn.execute("DELETE FROM search_fts WHERE rowid = ?", (rowid,))
    conn.execute("DELETE FROM search_docs WHERE rowid = ?", (rowid,))


def _index_file(conn, root: Path, rel: str, size: int, mtime: float) -> None:
    body = file_store.read_text_capped(file_store.resolve_note_path(root, rel), MAX_INDEX_BYTES)
    _upsert(conn, rel, body, size, mtime, source="note")


def _sources(conn, notes_root: Path) -> list[tuple[str, str, Path]]:
    """索引源：只有个人笔记目录（知识库改走 depot 归档，见 `kb_bases` / `sync_kb`）。"""
    return [("note", "", Path(notes_root))]


def kb_bases(conn) -> list[str]:
    """全部知识库名（索引源，与该库是否配置本机工作区无关）。"""
    try:
        rows = conn.execute("SELECT name FROM knowledge_bases ORDER BY name").fetchall()
    except sqlite3.Error:
        return []
    return [str(r["name"]) for r in rows if str(r["name"] or "").strip()]


def _scan(conn, source: str, kb_name: str, root: Path, *, progress: bool = False) -> int:
    """全量比对 mtime/size，只重读变化过的文件；清理磁盘上已消失的索引行（笔记源）。

    `progress=True` 时把处理进度写进 `_reindex_state`（供状态页显示进度条）。
    """
    if not root.exists():
        return 0
    touched = 0
    known = {
        r["note_path"]: (int(r["size"]), float(r["mtime"]))
        for r in conn.execute(
            "SELECT note_path, size, mtime FROM search_docs WHERE source = ? AND kb_name = ?",
            (source, kb_name),
        )
    }
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        prev = known.pop(rel, None)
        if prev is None or prev[0] != st.st_size or abs(prev[1] - st.st_mtime) >= 1e-6:
            try:
                _index_file(conn, root, rel, st.st_size, st.st_mtime)
            except (OSError, ValueError):
                pass
            else:
                touched += 1
                if touched % _COMMIT_EVERY == 0:
                    conn.commit()   # 及时释放写锁：全量扫描期间别把库锁住
        if progress:
            _bump_progress()
    for key in known:
        _drop(conn, key)
        touched += 1
        if touched % _COMMIT_EVERY == 0:
            conn.commit()
    return touched


def _bump_progress() -> None:
    with _lock:
        _reindex_state["done"] = int(_reindex_state.get("done", 0)) + 1


# ── 代码库索引源：本机目录（source="code"）──────────────────
#
# 与笔记/知识库并列的第三类索引源：把本机代码仓库目录纳入检索。
# 目录来自设置 `code_roots`（app_settings，运行时可改，无需重启），
# 扫描按 `CODE_EXTS` 过滤、跳过 `CODE_SKIP_DIRS`（依赖/构建产物/缓存），
# 命中统一带 source="code"、kb_name=label（目录名），可像知识库一样按库过滤/分面。


def _code_key(label: str, rel: str) -> str:
    return f"code:{label}:{rel}"


def code_roots(conn) -> list[tuple[str, Path]]:
    """已配置的代码库根：[(label, 绝对目录)]；label 默认取目录名（重名加序号）。"""
    try:
        from . import search_vec

        raw = search_vec.get_setting(conn, SETTING_CODE_ROOTS)
    except Exception:  # noqa: BLE001
        raw = ""
    out: list[tuple[str, Path]] = []
    seen: dict[str, int] = {}
    for part in re.split(r"[;\r\n]+", raw or ""):
        p = part.strip().strip('"')
        if not p:
            continue
        path = Path(p)
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        base = path.name or "code"
        n = seen.get(base, 0) + 1
        seen[base] = n
        label = base if n == 1 else f"{base}{n}"
        out.append((label, path))
    return out


def set_code_roots(conn, roots: list[str]) -> None:
    """写入代码库根目录设置（去重、去空），并提交。"""
    from . import search_vec

    clean: list[str] = []
    for r in roots:
        s = str(r or "").strip()
        if s and s not in clean:
            clean.append(s)
    search_vec.set_setting(conn, SETTING_CODE_ROOTS, "\n".join(clean))
    conn.commit()


def _scan_code(conn, label: str, root: Path, *, progress: bool = False) -> int:
    """扫描一个代码库目录（CODE_EXTS 过滤 + CODE_SKIP_DIRS 剪枝 + 大小上限）。"""
    if not root.exists():
        return 0
    known = {
        str(r["rel"]): (int(r["size"]), float(r["mtime"]))
        for r in conn.execute(
            "SELECT rel, size, mtime FROM search_docs WHERE source = 'code' AND kb_name = ?",
            (label,),
        )
    }
    touched = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in CODE_SKIP_DIRS and not d.endswith(".egg-info")
        ]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() not in CODE_EXTS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > MAX_INDEX_BYTES:
                continue
            rel = p.relative_to(root).as_posix()
            prev = known.pop(rel, None)
            if prev is None or prev[0] != st.st_size or abs(prev[1] - st.st_mtime) >= 1e-6:
                try:
                    body = file_store.read_text_capped(p, MAX_INDEX_BYTES)
                except (OSError, ValueError):
                    continue
                _upsert(conn, _code_key(label, rel), body, st.st_size, st.st_mtime,
                        source="code", kb_name=label, rel=rel, rev=0)
                touched += 1
                if touched % _COMMIT_EVERY == 0:
                    conn.commit()
            if progress:
                _bump_progress()
    for rel in known:   # 磁盘上已消失的
        _drop(conn, _code_key(label, rel))
        touched += 1
    return touched


# ── 知识库索引源：depot 已上传归档 ────────────────────────


def _kb_sync_one(conn, kb_name: str, *, progress: bool = False) -> int:
    """把某知识库的索引同步到 depot 归档现状（size/rev 未变的文件不下载内容）。

    两阶段：**先把要更新的内容全部下载到内存**（HTTP 期间不持有写事务），再逐篇写库并
    立即提交。否则下载几十秒期间写锁一直被占，别的请求写库会 `database is locked`
    （知识库总览页的 `ensure_default_base` 就是受害者）。
    """
    from . import depot_map

    tree = depot_map.list_tree(conn, kb_name, exts=WORKSPACE_EXTS)
    known = {
        str(r["rel"]): (int(r["size"]), int(r["rev"]))
        for r in conn.execute(
            "SELECT rel, size, rev FROM search_docs WHERE source = 'kb' AND kb_name = ?", (kb_name,)
        )
    }
    # 阶段 1：下载（不持有写事务）；单次有配额，剩下的本轮末尾再来
    fetched: list[tuple[str, int, int, str]] = []
    alive: set[str] = set()   # 归档里仍在的 rel（不在里面的索引行才算被删除）
    budget = _KB_FETCH_BYTES
    stopped_early = False
    for f in tree["files"]:
        rel = str(f["rel"])
        size, rev = int(f["size"]), int(f["rev"])
        alive.add(rel)
        if known.get(rel) == (size, rev):
            continue
        if len(fetched) >= _KB_FETCH_MAX_DOCS or budget <= 0:
            stopped_early = True
            break
        skip_key = (kb_name, rel)
        with _lock:
            if _kb_skip.get(skip_key) == rev:   # 该版本内容取不到（blob 缺失等），不反复重试
                continue
        try:
            text = depot_map.read_text(conn, kb_name, rel=rel, rev=rev, max_bytes=MAX_INDEX_BYTES)
        except depot_map.DepotError:
            with _lock:
                _kb_skip[skip_key] = rev
            continue
        with _lock:
            _kb_skip.pop(skip_key, None)
        fetched.append((rel, size, rev, text))
        budget -= len(text.encode("utf-8", "ignore"))
    with _lock:
        if stopped_early:
            _kb_more.add(kb_name)   # 还有没下载完的，本轮末尾继续
        else:
            _kb_more.discard(kb_name)
    # 阶段 2：写库（每篇一提交，写锁只占几十毫秒）
    touched = 0
    for rel, size, rev, text in fetched:
        _upsert(conn, _kb_key(kb_name, rel), text, size, 0.0,
                source="kb", kb_name=kb_name, rel=rel, rev=rev)
        conn.commit()
        touched += 1
        if progress:
            _bump_progress()
    if stopped_early:
        stale: list[str] = []   # 目录没列完，先不判"已删除"，免得误删还没比对到的行
    else:
        stale = [rel for rel in known if rel not in alive]
    for rel in stale:  # 归档里已删除 / 移走的文件
        _drop(conn, _kb_key(kb_name, rel))
        with _lock:
            _kb_skip.pop((kb_name, rel), None)
        touched += 1
        if touched % _COMMIT_EVERY == 0:
            conn.commit()
    if touched % _COMMIT_EVERY:
        conn.commit()
    return touched


def sync_kb(conn, kb_name: str, trigger: str = "") -> int:
    """同步单个知识库的归档索引（归档服务不可用只记状态，不抛给调用方）。

    `trigger`：`event`（提交/发布事件即时）/ `ticker`（TTL 兜底）/ `startup`（预热）。
    仅在**有变化**或**失败**时记入历史，避免每 5 分钟的兜底空转刷屏。
    """
    started = time.monotonic()
    try:
        changed = _kb_sync_one(conn, kb_name)
    except Exception as exc:  # noqa: BLE001 - depot 不可用不该影响检索
        msg = f"{type(exc).__name__}: {exc}"
        with _lock:
            _kb_errors[kb_name] = msg
        _record_history(conn, kind="kb_sync", target=kb_name, trigger=trigger,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        detail=f"失败: {msg}")
        return 0
    with _lock:
        _kb_errors.pop(kb_name, None)
        _kb_last_sync[kb_name] = time.monotonic()
    if changed:
        _record_history(conn, kind="kb_sync", target=kb_name, trigger=trigger, changed=changed,
                        duration_ms=int((time.monotonic() - started) * 1000))
    return changed


def notify_kb_change(kb_name: str = "") -> None:
    """知识库内容变化（上传 / 提交 / 发布 / 取消发布）→ 让后台线程即时同步。

    `kb_name` 为空表示「所有知识库」（如改了归档映射）。
    """
    with _lock:
        _kb_pending.add(str(kb_name or "").strip() or "*")
    _kb_wake.set()


def _kb_worker(db_path: Path, notes_root: Path) -> None:
    """知识库索引维护线程：事件即时同步 + KB_SCAN_TTL_S 兜底全量（列目录比对 rev）。"""
    from . import db as dbmod

    while True:
        woke = _kb_wake.wait(timeout=KB_SCAN_TTL_S)
        _kb_wake.clear()
        if woke:
            time.sleep(KB_DEBOUNCE_S)  # 防抖：连续提交合并成一次同步
        with _lock:
            names = set(_kb_pending)
            _kb_pending.clear()
        tick = not names  # 超时醒来 → 兜底扫全部（覆盖其它客户端上传的内容）
        try:
            conn = dbmod.connect(db_path)
        except Exception:  # noqa: BLE001
            continue
        try:
            todo = kb_bases(conn) if (tick or "*" in names) else sorted(n for n in names if n != "*")
            while todo:   # 命中下载配额的知识库排到本轮末尾继续，不必等下一次 tick
                name = todo.pop(0)
                sync_kb(conn, name, trigger="event" if woke else "ticker")
                with _lock:
                    more = name in _kb_more
                if more:
                    todo.append(name)
                    time.sleep(0.2)
        except Exception:  # noqa: BLE001 - 后台线程不因单次失败退出
            pass
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def ensure_kb_worker(db_path: Path, notes_root: Path) -> None:
    """启动知识库索引维护线程（幂等）。"""
    global _kb_started
    with _lock:
        if _kb_started:
            return
        _kb_started = True
    threading.Thread(
        target=_kb_worker, args=(Path(db_path), Path(notes_root)), name="search_kb", daemon=True
    ).start()


def warm_start(db_path: Path, notes_root: Path) -> bool:
    """启动预热：后台先全量比对笔记、再同步各知识库，让首个查询不再承担建索引耗时。"""
    with _lock:
        if _warm_state.get("running"):
            return False
        _warm_state.update({
            "running": True, "phase": "notes", "updated": 0,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": "", "error": "",
        })
    ensure_kb_worker(db_path, notes_root)
    threading.Thread(
        target=_warm_worker, args=(Path(db_path), Path(notes_root)), name="search_warm", daemon=True
    ).start()
    return True


def _warm_worker(db_path: Path, notes_root: Path) -> None:
    from . import db as dbmod

    try:
        conn = dbmod.connect(db_path)
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _warm_state.update({"running": False, "error": f"{type(exc).__name__}: {exc}"})
        return
    started = time.monotonic()
    try:
        updated = _refresh(conn, notes_root, force=True)
        with _lock:
            _warm_state["phase"] = "kb"
            _warm_state["updated"] = updated
        for name in kb_bases(conn):
            updated += sync_kb(conn, name, trigger="startup")
            with _lock:
                _warm_state["updated"] = updated
        with _lock:
            rest = bool(_kb_more)   # 命中下载配额的库交给维护线程收尾
        if rest:
            notify_kb_change("")
        _record_history(conn, kind="warm", trigger="startup", changed=updated,
                        duration_ms=int((time.monotonic() - started) * 1000), detail="启动预热")
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _warm_state["error"] = f"{type(exc).__name__}: {exc}"
        _record_history(conn, kind="warm", trigger="startup",
                        duration_ms=int((time.monotonic() - started) * 1000),
                        detail=f"失败: {type(exc).__name__}: {exc}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        with _lock:
            _warm_state["running"] = False
            _warm_state["finished_at"] = datetime.now().isoformat(timespec="seconds")


def warm_state() -> dict[str, Any]:
    """启动预热状态快照。"""
    with _lock:
        return dict(_warm_state)


def _count_disk_files(root: Path, source: str) -> int:
    """磁盘上「应当被索引」的文件数（用于重建进度总量）。"""
    if not root.exists():
        return 0
    total = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if source == "kb" and p.suffix.lower() not in WORKSPACE_EXTS:
            continue
        total += 1
    return total


def _count_kb_files(conn, kb_name: str) -> int:
    """归档里「应当被索引」的文件数（0 = 归档服务不可用，只影响进度条总量）。"""
    from . import depot_map

    try:
        return len(depot_map.list_tree(conn, kb_name, exts=WORKSPACE_EXTS)["files"])
    except Exception:  # noqa: BLE001
        return 0


def refresh(conn, notes_root: Path, *, force: bool = False) -> int:
    """把索引同步到磁盘现状，返回本次更新/删除的文档数。

    查询前调用：正常情况下只处理少量标脏文件（或直接空转），不会读全部文档。
    进程内串行执行，避免并发请求重复索引同一文件；后台重建进行中则直接跳过
    （重建任务负责全量，查询照样读已有索引，不会被阻塞）。
    """
    if _reindex_state.get("running"):
        return 0
    with _refresh_lock:
        return _refresh(conn, notes_root, force=force)


def _refresh(conn, notes_root: Path, *, force: bool = False) -> int:
    root = Path(notes_root)
    with _lock:
        pending = dict(_pending)
        _pending.clear()
        now = time.monotonic()
        due = force or now - _last_scan.get(str(root), 0.0) >= SCAN_TTL_S
        if due:
            _last_scan[str(root)] = now
    touched = 0
    # 1) 服务内增删的笔记（钩子标脏）→ 只处理这几个文件
    for rel, action in pending.items():
        try:
            p = file_store.resolve_note_path(root, rel)
        except ValueError:
            continue
        if action == "delete" or not p.is_file():
            _drop(conn, rel)
            touched += 1
            continue
        try:
            st = p.stat()
            _index_file(conn, root, rel, st.st_size, st.st_mtime)
        except (OSError, ValueError):
            continue
        touched += 1
    # 2) TTL 全量比对（笔记目录 + 代码库；知识库归档由后台线程按事件/TTL 同步，请求路径不走网络）
    if due:
        for source, kb_name, src_root in _sources(conn, root):
            touched += _scan(conn, source, kb_name, src_root)
        for label, croot in code_roots(conn):
            touched += _scan_code(conn, label, croot)
    if touched:
        conn.commit()
    return touched


def _rebuild(conn, notes_root: Path, *, progress: bool = False) -> int:
    """清空并全量重建索引（笔记目录 + 各知识库归档）。"""
    with _refresh_lock:
        conn.execute("DELETE FROM search_fts")
        conn.execute("DELETE FROM search_docs")
        conn.commit()
        with _lock:
            _pending.clear()
            _reindex_state["phase"] = "counting"
            if progress:
                _reindex_state["done"] = 0
                _reindex_state["total"] = sum(
                    _count_disk_files(root, source)
                    for source, _kb_name, root in _sources(conn, Path(notes_root))
                ) + sum(_count_kb_files(conn, name) for name in kb_bases(conn))
        touched = 0
        with _lock:
            _reindex_state["phase"] = "indexing"
        for source, kb_name, root in _sources(conn, Path(notes_root)):
            touched += _scan(conn, source, kb_name, root, progress=progress)
            conn.commit()
        for label, croot in code_roots(conn):
            touched += _scan_code(conn, label, croot, progress=progress)
            conn.commit()
        for name in kb_bases(conn):
            touched += _kb_sync_one(conn, name, progress=progress)
            conn.commit()
        return touched


def reindex(conn, notes_root: Path) -> int:
    """同步重建全部索引（笔记目录 + 各知识库归档），返回写入/删除的索引行数。"""
    started = time.monotonic()
    touched = _rebuild(conn, notes_root)
    _record_history(conn, kind="rebuild", trigger="manual", changed=touched, total=touched,
                    duration_ms=int((time.monotonic() - started) * 1000), detail="同步重建全部索引")
    return touched


def start_reindex(db_path: Path, notes_root: Path) -> bool:
    """后台线程重建全部索引（状态页用）。返回是否已启动（已在跑则 False）。"""
    with _lock:
        if _reindex_state.get("running"):
            return False
        _reindex_state.update(
            {
                "running": True,
                "phase": "counting",
                "total": 0,
                "done": 0,
                "updated": 0,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": "",
                "error": "",
            }
        )
    thread = threading.Thread(
        target=_reindex_worker, args=(Path(db_path), Path(notes_root)), name="search_reindex", daemon=True
    )
    thread.start()
    return True


def _reindex_worker(db_path: Path, notes_root: Path) -> None:
    from . import db as dbmod

    conn = dbmod.connect(db_path)
    started = time.monotonic()
    try:
        updated = _rebuild(conn, notes_root, progress=True)
        with _lock:
            _reindex_state["updated"] = updated
        _record_history(conn, kind="rebuild", trigger="manual_async", changed=updated, total=updated,
                        duration_ms=int((time.monotonic() - started) * 1000), detail="后台重建全部索引")
    except Exception as exc:  # noqa: BLE001 - 状态页展示错误即可
        with _lock:
            _reindex_state["error"] = f"{type(exc).__name__}: {exc}"
        _record_history(conn, kind="rebuild", trigger="manual_async",
                        duration_ms=int((time.monotonic() - started) * 1000),
                        detail=f"失败: {type(exc).__name__}: {exc}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        with _lock:
            _reindex_state["running"] = False
            _reindex_state["finished_at"] = datetime.now().isoformat(timespec="seconds")


def reindex_state() -> dict[str, Any]:
    """后台重建任务的当前状态快照（深拷贝，避免调用方读到半更新值）。"""
    with _lock:
        return dict(_reindex_state)


# ── 重建/同步历史（search_history 表）─────────────────────


def _record_history(conn, *, kind: str, target: str = "", trigger: str = "",
                    changed: int = 0, total: int = 0, duration_ms: int = 0,
                    detail: str = "") -> None:
    """记录一次索引重建/同步事件（保留最近 HISTORY_KEEP 条）。

    历史是旁路信息：任何异常都不应影响索引主流程。
    """
    try:
        conn.execute(
            "INSERT INTO search_history(at, kind, target, trigger, changed, total, duration_ms, detail)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), kind, target, trigger,
             int(changed), int(total), int(duration_ms), detail),
        )
        conn.execute(
            "DELETE FROM search_history WHERE id NOT IN"
            " (SELECT id FROM search_history ORDER BY id DESC LIMIT ?)",
            (HISTORY_KEEP,),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 - 历史记录失败不影响索引
        pass


def history(conn, limit: int = 50) -> list[dict[str, Any]]:
    """最近的索引重建/同步历史（倒序）。"""
    try:
        rows = conn.execute(
            "SELECT id, at, kind, target, trigger, changed, total, duration_ms, detail"
            " FROM search_history ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    except Exception:  # noqa: BLE001 - 旧库未建表时返回空
        return []
    return [dict(r) for r in rows]


def _history_total(conn) -> int:
    """历史总条数。"""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0


# ── 状态统计 ────────────────────────────────────────────


def _vec_stats(conn) -> dict[str, Any]:
    """向量索引状态（未启用/缺表时返回 available=False）。"""
    try:
        from . import search_vec
    except Exception as exc:  # noqa: BLE001
        return {"available": {"available": False, "reason": f"{type(exc).__name__}: {exc}"}}
    try:
        return search_vec.stats(conn)
    except Exception as exc:  # noqa: BLE001
        return {"available": {"available": False, "reason": f"{type(exc).__name__}: {exc}"}}


def _rerank_stats(conn) -> dict[str, Any]:
    """重排状态（容错：取不到时返回不可用状态，不影响状态页其它字段）。"""
    try:
        from . import search_vec
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "enabled": False, "reason": f"{type(exc).__name__}: {exc}"}
    try:
        return search_vec.rerank_status(conn)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "enabled": False, "reason": f"{type(exc).__name__}: {exc}"}


def _verify_notes(conn, root: Path) -> dict[str, int]:
    """笔记源磁盘校对：磁盘文件数 / 未索引 / mtime 过期 / 多余行。"""
    indexed = {
        r["rel"]: (int(r["size"]), float(r["mtime"]))
        for r in conn.execute("SELECT rel, size, mtime FROM search_docs WHERE source = 'note'")
    }
    disk = missing = changed = 0
    seen: set[str] = set()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        disk += 1
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        prev = indexed.get(rel)
        if prev is None:
            missing += 1
        elif prev[0] != st.st_size or abs(prev[1] - st.st_mtime) >= 1e-6:
            changed += 1
    return {
        "disk_files": disk,
        "missing": missing,
        "changed": changed,
        "extra": len([k for k in indexed if k not in seen]),
    }


def _verify_kb(conn, kb_name: str) -> dict[str, Any]:
    """归档校对：列 depot 目录比对 rev（网络失败只返回错误，不算差异）。"""
    from . import depot_map

    indexed = {
        str(r["rel"]): (int(r["size"]), int(r["rev"]))
        for r in conn.execute(
            "SELECT rel, size, rev FROM search_docs WHERE source = 'kb' AND kb_name = ?", (kb_name,)
        )
    }
    try:
        files = depot_map.list_tree(conn, kb_name, exts=WORKSPACE_EXTS)["files"]
    except Exception as exc:  # noqa: BLE001
        return {"disk_files": len(indexed), "missing": 0, "changed": 0, "extra": 0,
                "verify_error": f"{type(exc).__name__}: {exc}"}
    missing = changed = unavailable = 0
    seen: set[str] = set()
    for f in files:
        rel = str(f["rel"])
        seen.add(rel)
        prev = indexed.get(rel)
        if prev is None:
            with _lock:
                skipped = _kb_skip.get((kb_name, rel)) == int(f["rev"])
            if skipped:
                unavailable += 1   # 归档有元数据但 blob 取不到（同步时已跳过，不算缺失）
            else:
                missing += 1
        elif prev != (int(f["size"]), int(f["rev"])):
            changed += 1
    return {"disk_files": len(files), "missing": missing, "changed": changed,
            "unavailable": unavailable,
            "extra": len([k for k in indexed if k not in seen])}


def _kb_source_rows(conn, by_source) -> list[dict[str, Any]]:
    """知识库源明细（索引文档数来自库表，根路径来自归档映射，不访问网络）。"""
    from . import depot_map

    rows: list[dict[str, Any]] = []
    for name in kb_bases(conn):
        row = by_source.get(("kb", name))
        try:
            base = depot_map.mapping(conn, name)["base_path"]
        except Exception:  # noqa: BLE001 - 知识库不存在等
            base = f"/notes/{name}"
        with _lock:
            err = _kb_errors.get(name, "")
            last = _kb_last_sync.get(name)
        rows.append({
            "source": "kb",
            "kb_name": name,
            "root": f"depot:{base}",
            "exists": True,
            "docs": int(row["docs"]) if row else 0,
            "bytes": int(row["bytes"] or 0) if row else 0,
            "last_indexed_at": (row["last_indexed_at"] if row else "") or "",
            "newest_mtime": "",   # 归档按 rev 而非 mtime 增量
            "last_sync_ago": round(time.monotonic() - last, 1) if last is not None else None,
            "error": err,
        })
    return rows


def stats(conn, notes_root: Path, *, deep: bool = False) -> dict[str, Any]:
    """索引状态：总量、分来源明细、增量队列、启动预热与后台重建进度。

    `deep=True` 时额外校对（笔记比对磁盘、知识库比对归档 rev）与 FTS 完整性检查。
    """
    root = Path(notes_root)
    docs = conn.execute("SELECT COUNT(*) AS c FROM search_docs").fetchone()["c"]
    fts_rows = conn.execute("SELECT COUNT(*) AS c FROM search_fts").fetchone()["c"]
    page = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]

    by_source = {
        (r["source"], r["kb_name"]): r
        for r in conn.execute(
            "SELECT source, kb_name, COUNT(*) AS docs, SUM(size) AS bytes,"
            " MAX(indexed_at) AS last_indexed_at, MAX(mtime) AS newest_mtime"
            " FROM search_docs GROUP BY source, kb_name"
        )
    }

    sources: list[dict[str, Any]] = []
    for source, kb_name, src_root in _sources(conn, root):
        row = by_source.get((source, kb_name))
        item: dict[str, Any] = {
            "source": source,
            "kb_name": kb_name,
            "root": str(src_root),
            "exists": src_root.is_dir(),
            "docs": int(row["docs"]) if row else 0,
            "bytes": int(row["bytes"] or 0) if row else 0,
            "last_indexed_at": (row["last_indexed_at"] if row else "") or "",
            "newest_mtime": _iso(float(row["newest_mtime"])) if row and row["newest_mtime"] else "",
        }
        if deep and item["exists"]:
            item.update(_verify_notes(conn, src_root))
        sources.append(item)

    kb_rows = _kb_source_rows(conn, by_source)
    if deep:
        for item in kb_rows:
            item.update(_verify_kb(conn, str(item["kb_name"])))
    sources.extend(kb_rows)

    for label, croot in code_roots(conn):
        row = by_source.get(("code", label))
        sources.append({
            "source": "code",
            "kb_name": label,
            "root": str(croot),
            "exists": croot.is_dir(),
            "docs": int(row["docs"]) if row else 0,
            "bytes": int(row["bytes"] or 0) if row else 0,
            "last_indexed_at": (row["last_indexed_at"] if row else "") or "",
            "newest_mtime": _iso(float(row["newest_mtime"])) if row and row["newest_mtime"] else "",
        })

    with _lock:
        kb_pending = sorted(_kb_pending)

    try:
        from . import workspace_sync
        ws_autosync: dict[str, Any] = workspace_sync.status()
    except Exception as exc:  # noqa: BLE001 - 状态页容错
        ws_autosync = {"enabled": False, "reason": f"{type(exc).__name__}: {exc}"}

    result: dict[str, Any] = {
        "docs": int(docs),
        "vec": _vec_stats(conn),
        "rerank": _rerank_stats(conn),
        "fts_rows": int(fts_rows),
        "fts_consistent": int(docs) == int(fts_rows),
        "db_bytes": int(page) * int(page_size),
        "sources": sources,
        "pending": len(_pending),
        "scan_ttl_s": SCAN_TTL_S,
        "last_scan_ago": (
            round(time.monotonic() - _last_scan[str(root)], 1) if str(root) in _last_scan else None
        ),
        "max_index_bytes": MAX_INDEX_BYTES,
        "workspace_exts": sorted(WORKSPACE_EXTS),
        "code_exts": sorted(CODE_EXTS),
        "code_roots": [{"label": lb, "root": str(rp)} for lb, rp in code_roots(conn)],
        "kb_scan_ttl_s": KB_SCAN_TTL_S,
        "kb_pending": kb_pending,
        "warm": warm_state(),
        "reindex": reindex_state(),
        "history": history(conn, limit=20),
        "history_total": _history_total(conn),
        "workspace_sync": ws_autosync,
        "deep": bool(deep),
    }
    if deep:
        try:
            conn.execute("INSERT INTO search_fts(search_fts) VALUES('integrity-check')")
            conn.commit()  # 该语句会开启写事务，及时收尾，避免挡住后台重建
            result["fts_integrity"] = "ok"
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            result["fts_integrity"] = f"{type(exc).__name__}: {exc}"
    return result


__all__ = [
    "install",
    "refresh",
    "reindex",
    "reindex_state",
    "start_reindex",
    "search",
    "stats",
    "highlight",
    "parse_query",
    "build_match_expr",
    "index_text",
    "kb_bases",
    "sync_kb",
    "notify_kb_change",
    "ensure_kb_worker",
    "warm_start",
    "warm_state",
    "MAX_INDEX_BYTES",
    "WORKSPACE_EXTS",
]


# ── 检索 ────────────────────────────────────────────────


def _rerank_decision(conn, rerank: bool | None) -> tuple[bool, str]:
    """本次请求是否使用重排，以及未使用时的说明（供接口/页面对照）。"""
    try:
        from . import search_vec
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    try:
        configured = search_vec.rerank_configured()
        enabled = search_vec.rerank_enabled(conn)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if not configured:
        return False, "未配置 L_NOTEPAD_RERANK_URL"
    if not enabled:
        return False, "重排已关闭"
    if rerank is False:
        return False, "本次请求关闭（rerank=0）"
    return True, ""


def _best_chunk(chunks: list[tuple[int, int, str]], units: list[dict[str, str]]) -> tuple[int, int, str]:
    """在文档的块里挑词法最匹配的一块（短语命中优先，再按覆盖率/词频分）。"""
    best_no, best_start, best_text = 0, 0, ""
    best_key: tuple[int, float] | None = None
    for no, start, text in chunks:
        meta = _score(text.lower(), units, 0.0)
        key = (meta["phrase_hits"], float(meta["score"]))
        if best_key is None or key > best_key:
            best_no, best_start, best_text, best_key = no, start, text, key
    return best_no, best_start, best_text


def _chunk_map(
    conn, query: str, keys: list[str], units: list[dict[str, str]], *, semantic: bool
) -> dict[str, tuple[int, int, str, float]]:
    """每篇命中文档的「命中块」：{key: (块序号, 偏移, 块文本, 向量分)}。

    语义可用时优先取向量的最优块；其余（仅词法模式、或该文档不在语义候选里）按词法在块文本里选最优块 ——
    两种情况都不额外发起 embedding 请求。无块向量的文档不出现在结果里。
    """
    if not keys:
        return {}
    try:
        from . import search_vec
    except Exception:  # noqa: BLE001 - 块级信息是增强，取不到不影响检索
        return {}
    wanted = set(keys)
    out: dict[str, tuple[int, int, str, float]] = {}
    if semantic:
        try:
            for key, no, start, text, score in search_vec.search_chunks(
                conn, query, limit=max(len(wanted) * 2, 40)
            ):
                if key in wanted and key not in out:
                    out[key] = (no, start, text, score)
        except Exception:  # noqa: BLE001
            pass
    missing = [k for k in keys if k not in out]
    if missing:
        try:
            for key, chunks in search_vec.doc_chunks(conn, missing).items():
                if not chunks:
                    continue
                no, start, text = _best_chunk(chunks, units)
                if text:
                    out[key] = (no, start, text, 0.0)
        except Exception:  # noqa: BLE001
            pass
    return out


def _apply_rerank(
    conn, query: str, hits: list[dict[str, Any]], *, allow: bool, off_reason: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """候选块重排：已重排按重排分主序，未参与重排的候选垫底并保持原相对顺序。

    重排分不并入 `score`（cross-encoder 分与线性融合分量纲不可比），只作为排序主序与独立字段。
    """
    info: dict[str, Any] = {"used": False, "model": "", "scored": 0, "took_ms": 0.0, "reason": off_reason}
    if not allow or not hits:
        return hits, info
    try:
        from . import search_vec
    except Exception as exc:  # noqa: BLE001
        info["reason"] = f"{type(exc).__name__}: {exc}"
        return hits, info
    top_n = max(1, int(search_vec.RERANK_TOP_N))
    cand = [h for h in hits if h.get("chunk")][:top_n]
    if not cand:
        info["reason"] = "候选无命中块（无块向量）"
        return hits, info
    scores, sub = search_vec.rerank_docs(conn, query, [str(h["chunk"]) for h in cand])
    info.update(sub)
    if not scores:
        return hits, info
    smap = dict(scores)
    for i, hit in enumerate(cand):
        hit["rerank"] = round(float(smap.get(i, 0.0)), 4)
    ranked = sorted(cand, key=lambda h: (-h["rerank"], -h["score"], h["path"]))
    ranked_ids = {id(h) for h in ranked}
    return ranked + [h for h in hits if id(h) not in ranked_ids], info


def search(
    conn,
    notes_root: Path,
    query: str,
    *,
    user: str,
    admin: bool = False,
    limit: int = 20,
    offset: int = 0,
    sources: list[str] | None = None,
    mode: str = "hybrid",
    kb_name: str = "",
    rerank: bool | None = None,
) -> dict[str, Any]:
    """倒排检索（只查索引表，不读文档）。

    宽召回 + 重排：同段 bigram OR 召回，再按「短语命中 > 覆盖率 > 词频/近邻 > bm25」加权评分排序，
    最后按需用本地重排服务对候选块做交叉编码重排（不可用时静默退回融合排序）。
    `"引号"` 精确短语无结果时自动回退为整串模糊匹配（`fallback=True`）。
    `rerank`：None 用全局配置，False 本次关闭（全局关闭时传 True 也不生效）。
    返回 {total, hits, took_ms, fallback, vec, rerank}；hit 含 source（note/kb）、kb_name、rel、path、
    snippet、matches、chunk、chunk_no、chunk_offset、rerank、coverage、tf、proximity、phrase_hits、bm25、score。
    """
    empty: dict[str, Any] = {
        "total": 0, "hits": [], "took_ms": 0.0, "fallback": False,
        "vec": {"used": False, "model": "", "hits": 0},
        "rerank": {"used": False, "model": "", "scored": 0, "took_ms": 0.0, "reason": ""},
    }
    units = parse_query(query or "")
    if not units:
        return empty
    match = build_match_expr(units)
    if not match:
        return empty

    refresh(conn, notes_root)
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    offset = max(0, int(offset or 0))

    src_clause = ""
    params: dict[str, Any] = {"q": match, "user": user, "admin": 1 if admin else 0}
    if sources:
        names = ",".join(f":src{i}" for i in range(len(sources)))
        src_clause = f" AND search_docs.source IN ({names})"
        params.update({f"src{i}": s for i, s in enumerate(sources)})
    if kb_name:
        src_clause += " AND search_docs.kb_name = :kb_name"
        params["kb_name"] = kb_name

    started = time.perf_counter()
    # 候选：按 bm25 取 (offset+limit)*K 条，再按覆盖率/短语重排（保证相关性优先）
    cand = min((offset + limit) * _CANDIDATE_FACTOR, MAX_LIMIT * _CANDIDATE_FACTOR)
    total, rows = _run_query(conn, match, params, src_clause, cand)

    fallback = False
    if total == 0 and any(u["type"] == "phrase" for u in units):
        # 精确短语无结果 → 整串按普通模糊再试一次（搜索引擎的"未找到精确匹配，已显示模糊结果"）
        loose_units = parse_query(query or "", loose=True)
        loose_match = build_match_expr(loose_units) if loose_units else ""
        if loose_units != units and loose_match:
            total2, rows2 = _run_query(
                conn, loose_match, {**params, "q": loose_match}, src_clause, cand
            )
            if total2:
                units, total, rows, fallback = loose_units, total2, rows2, True

    # 语义（向量）召回：与词法结果融合；纯语义命中的文档也补进来
    vmap: dict[str, float] = {}
    vinfo: dict[str, Any] = {"used": False, "model": "", "hits": 0}
    if mode in ("hybrid", "sem"):
        vmap, vinfo = _vec_scores(conn, query, user=user, admin=admin, sources=sources, kb_name=kb_name)
        if mode == "sem" and not vmap:
            # 纯语义模式且语义无命中：不要回退成"词法 total 但列表为空"的误导结果
            reason = _rerank_decision(conn, rerank)[1] or "语义无命中，无可重排候选"
            return {**empty, "took_ms": round((time.perf_counter() - started) * 1000, 2),
                    "vec": vinfo, "rerank": {**empty["rerank"], "reason": reason}}
        if mode == "sem":
            # 纯语义：只留有语义分的文档，按相似度排序（词法行用作补全 body/snippet）
            rows = [r for r in rows if r["key"] in vmap] + _vec_only_rows(
                conn, vmap, [r["key"] for r in rows], user=user, admin=admin,
                sources=sources, kb_name=kb_name,
            )
        elif vmap and total == 0:
            # 混合：词法为空时用语义兜底
            rows = list(rows) + _vec_only_rows(
                conn, vmap, [r["key"] for r in rows], user=user, admin=admin,
                sources=sources, kb_name=kb_name,
            )

    if total == 0 and not vmap:
        reason = _rerank_decision(conn, rerank)[1] or "无命中文档"
        return {**empty, "took_ms": round((time.perf_counter() - started) * 1000, 2),
                "rerank": {**empty["rerank"], "reason": reason}}
    total = len(vmap) if mode == "sem" and vmap else max(total, len(rows))

    # 命中块（块级信息 + 重排候选）：语义优先，取不到时按词法在块文本里选
    chunk_map = _chunk_map(
        conn, query, [r["key"] for r in rows], units, semantic=mode in ("hybrid", "sem")
    )

    hits: list[dict[str, Any]] = []
    for r in rows:
        meta = _score((r["body"] or "").lower(), units, float(r["rank"]))
        vec = vmap.get(r["key"], 0.0)
        if vec:
            meta["vec"] = round(vec, 4)
            meta["score"] = round(vec if mode == "sem" else meta["score"] + _W_VEC * vec, 4)
        chunk_no, chunk_start, chunk_text, _chunk_vec = chunk_map.get(r["key"], (0, 0, "", 0.0))
        snippet, matches = _snippet(r["body"], units, chunk_text=chunk_text, chunk_offset=chunk_start)
        hits.append(
            {
                "path": r["rel"],
                "source": r["source"],
                "kb_name": r["kb_name"],
                "rel": r["rel"],
                "snippet": snippet,
                "matches": matches,
                "chunk": chunk_text,
                "chunk_no": int(chunk_no),
                "chunk_offset": int(chunk_start),
                "rerank": 0.0,
                "updated_at": _iso(r["mtime"]),
                "coverage": meta["coverage"],
                "tf": meta["tf"],
                "proximity": meta["proximity"],
                "bm25": round(float(r["rank"]), 4),
                "vec": meta.get("vec", 0.0),
                "phrase_hits": meta["phrase_hits"],
                "score": meta["score"],
            }
        )
    if mode == "sem":
        hits.sort(key=lambda h: (-h["score"], h["path"]))
    else:
        hits.sort(key=lambda h: (-h["phrase_hits"], -h["score"], h["path"]))

    # 重排（本地 cross-encoder）：重排分作主序，未参与重排的候选垫底
    allow_rerank, off_reason = _rerank_decision(conn, rerank)
    hits, rinfo = _apply_rerank(conn, query, hits, allow=allow_rerank, off_reason=off_reason)

    page = hits[offset : offset + limit]
    return {
        "total": total,
        "hits": page,
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
        "fallback": fallback,
        "vec": vinfo,
        "rerank": rinfo,
    }


def search_auto(
    conn,
    notes_root: Path,
    query: str,
    *,
    user: str,
    admin: bool = False,
    limit: int = 20,
    offset: int = 0,
    sources: list[str] | None = None,
    rerank: bool | None = None,
    kb_name: str = "",
) -> dict[str, Any]:
    """词法优先、零命中回退 hybrid —— `mode=auto` 的入口。

    长句 / 自然语言常因多子句被段间 AND 拆开而词法零命中；先 `lex`（毫秒级），
    只在 `hits` 为空时再做一次 `hybrid`（语义兜底），避免界面显示"无命中"。
    """
    result = search(
        conn, notes_root, query, user=user, admin=admin, limit=limit, offset=offset,
        sources=sources, mode="lex", rerank=rerank, kb_name=kb_name,
    )
    if result.get("hits") or int(result.get("total") or 0) > 0:
        result["mode_used"] = "lex"
        return result
    result = search(
        conn, notes_root, query, user=user, admin=admin, limit=limit, offset=offset,
        sources=sources, mode="hybrid", rerank=rerank, kb_name=kb_name,
    )
    result["mode_used"] = "hybrid"
    return result


# ── 快速选库路由（/api/search/route）───────────────────────
#
# 目标：复杂需求 → 相关知识库排序，供 Agent 决定读哪几个库。
# 硬约束：必须快。请求路径只读索引表与元数据表，**不做语义探测、不访问网络**，
# 绝不触发 `search()` 的 hybrid 兜底（无 ollama 时那里会反复探测 11434，约 6 秒）。


def _meta_bases(conn, terms: list[str]) -> list[dict[str, Any]]:
    """库元数据（name/title/description）与关键词块的重叠命中。"""
    if not terms:
        return []
    try:
        from . import knowledge

        bases = knowledge.list_bases(conn)
    except Exception:  # noqa: BLE001 知识库表尚未建好时不参与选库
        return []
    tset = set(terms)
    out: list[dict[str, Any]] = []
    for b in bases:
        name = str(b.get("name") or "")
        text = " ".join(
            (name, str(b.get("title") or ""), str(b.get("description") or ""))
        )
        hits = len(tset & set(_tokens(text)))
        if hits:
            out.append({"kb_name": name, "meta_hits": hits})
    return out


def _kb_aggregate(
    conn,
    notes_root: Path,
    terms: list[str],
    *,
    user: str,
    admin: bool,
    sources: list[str] | None,
    refresh_index: bool = True,
) -> dict[str, dict[str, Any]]:
    """词法按库聚合：一次 GROUP BY kb_name 拿到各库命中量与最优 bm25。

    `refresh_index=False`（选库快路径）跳过 TTL 全量比对——选库容忍几秒陈旧，
    换掉每次可能 stat 整个笔记目录的开销。
    """
    if not terms:
        return {}
    match = route_match_expr(terms)
    if not match:
        return {}
    if refresh_index:
        refresh(conn, notes_root)
    params: dict[str, Any] = {"q": match, "user": user, "admin": 1 if admin else 0}
    src_clause = _src_clause(sources, params, prefix="rsrc")
    rows = conn.execute(
        "SELECT search_docs.kb_name AS kb_name, COUNT(*) AS doc_hits,"
        " MIN(search_fts.rank) AS best_rank"
        " FROM search_fts JOIN search_docs ON search_docs.rowid = search_fts.rowid"
        f" WHERE search_fts MATCH :q {_PERM_SQL}{src_clause}"
        " GROUP BY search_docs.kb_name",
        params,
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = str(r["kb_name"] or "")
        if not name:
            continue
        out[name] = {
            "doc_hits": int(r["doc_hits"] or 0),
            "best_bm25": round(float(r["best_rank"] or 0.0), 4),
        }
    return out


def _kb_semantic(conn, query: str) -> dict[str, float]:
    """depth=2 语义加分：按库取库内文档最高语义相似度（不可用 → 空，绝不阻塞）。

    先按模型阈值过滤，避免弱相似（余弦 0.4~0.5）把无关库抬进结果。
    """
    try:
        from . import search_vec
    except Exception:  # noqa: BLE001
        return {}
    try:
        scores = search_vec.kb_semantic_scores(conn, query)
        if not scores:
            return {}
        floor = float(search_vec.vec_min(search_vec.model(conn)))
    except Exception:  # noqa: BLE001
        return {}
    return {k: v for k, v in scores.items() if v >= floor}


_route_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}


def _route_cache_get(key: tuple) -> dict[str, Any] | None:
    item = _route_cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.monotonic() - ts > _ROUTE_CACHE_TTL:
        _route_cache.pop(key, None)
        return None
    return val


def _route_cache_put(key: tuple, val: dict[str, Any]) -> None:
    if key not in _route_cache and len(_route_cache) >= _ROUTE_CACHE_MAX:
        oldest = min(_route_cache, key=lambda k: _route_cache[k][0])
        _route_cache.pop(oldest, None)
    _route_cache[key] = (time.monotonic(), dict(val))


def route(
    conn,
    notes_root: Path,
    query: str,
    *,
    user: str,
    admin: bool = False,
    depth: int = 1,
    limit: int = 10,
    sources: list[str] | None = None,
    budget_ms: int = 0,
) -> dict[str, Any]:
    """快速选库：复杂需求 → 相关知识库排序（毫秒级，见 routers/search.py `/route`）。

    depth 分档：
      0 —— 仅库元数据匹配（name/title/description）
      1 —— 元数据 + FTS5 词法按库聚合（默认，量级 ~毫秒）
      2 —— 语义档：需求嵌一次，与**各库摘要向量**（元数据 + 文档质心）比对作为加分；
            语义不可用 / 无命中 → **自动降级为 1** 并在 reason 说明（0.3s TCP 探测兜底，不阻塞）
      3 —— 不处理：需求分解 / 多查询由调用方自行完成（返回空 kbs + 说明）

    返回 {depth_req, depth_used, degraded, reason, reason_code, took_ms, cached, terms, kbs}；
    kbs 按 score 降序（score = 0.5*log1p(doc_hits) + 2.5*meta_hits + 1.5*vec + 1.5*tanh(-bm25/20)）。
    同一 (q, req, budget, limit, sources, user) 结果缓存 `_ROUTE_CACHE_TTL` 秒（`cached=true`）。
    `budget_ms>0 且 <100` 时 `depth>=2` 自动退回 `1`（`reason_code=budget_downgrade`）。
    """
    started = time.perf_counter()
    req = int(depth) if depth is not None else 1
    req = max(0, min(req, 3))
    req_orig = req
    budget = max(0, int(budget_ms or 0))
    limit = max(1, min(int(limit or 10), 50))
    src_key = tuple(sources or ())

    cache_key = (query, req_orig, budget, limit, src_key, user, 1 if admin else 0)
    hit = _route_cache_get(cache_key)
    if hit is not None:
        return {**hit, "cached": True,
                "took_ms": round((time.perf_counter() - started) * 1000, 2)}

    degraded = False
    reason = ""
    reason_code = "ok"
    if req >= 3:
        used = 3
        degraded = True
        reason = "depth>=3 需要需求分解/多查询，请调用方自行处理"
        reason_code = "delegate"
    elif req == 2 and budget and budget < 100:
        used = 1
        degraded = True
        reason = "budget_ms<100，跳过语义档，按 depth=1 返回"
        reason_code = "budget_downgrade"
    else:
        used = req

    if used == 3:
        result: dict[str, Any] = {
            "query": query, "depth_req": req_orig, "depth_used": 3,
            "degraded": True, "reason": reason, "reason_code": reason_code,
            "took_ms": round((time.perf_counter() - started) * 1000, 2),
            "cached": False, "terms": [], "kbs": [],
        }
        _route_cache_put(cache_key, result)
        return result

    terms = route_terms(query)
    entries: dict[str, dict[str, Any]] = {}
    include_kb = (not sources) or ("kb" in sources)

    if used >= 1:
        agg = _kb_aggregate(
            conn, notes_root, terms, user=user, admin=admin, sources=sources,
            refresh_index=False,
        )
        meta = (
            {m["kb_name"]: m for m in _meta_bases(conn, terms)} if include_kb else {}
        )
        for name, info in agg.items():
            entries[name] = {
                "kb_name": name,
                "doc_hits": info["doc_hits"],
                "best_bm25": info["best_bm25"],
                "meta_hits": int(meta.get(name, {}).get("meta_hits", 0)),
                "vec": 0.0,
            }
        # 元数据命中但索引里没有的库也保留（归档未连通时索引可能为空）
        for name, m in meta.items():
            if name not in entries:
                entries[name] = {
                    "kb_name": name,
                    "doc_hits": 0,
                    "best_bm25": 0.0,
                    "meta_hits": int(m["meta_hits"]),
                    "vec": 0.0,
                }
    elif include_kb:   # depth 0：只靠元数据
        for m in _meta_bases(conn, terms):
            entries[m["kb_name"]] = {
                "kb_name": m["kb_name"],
                "doc_hits": 0,
                "best_bm25": 0.0,
                "meta_hits": int(m["meta_hits"]),
                "vec": 0.0,
            }

    # depth=2：语义加分（需求嵌一次，与各库摘要向量比对）
    if used == 2:
        vs = _kb_semantic(conn, query)
        if vs:
            for name, v in vs.items():
                e = entries.get(name)
                if e is None:
                    e = entries[name] = {
                        "kb_name": name,
                        "doc_hits": 0,
                        "best_bm25": 0.0,
                        "meta_hits": 0,
                        "vec": 0.0,
                    }
                e["vec"] = round(float(v), 4)
        else:
            used = 1
            degraded = True
            reason = "语义不可用或无语义命中，已降级为 depth=1"
            reason_code = "semantic_degraded"

    for e in entries.values():
        e["score"] = round(
            _ROUTE_W_DOCS * math.log1p(e["doc_hits"])
            + _ROUTE_W_META * e["meta_hits"]
            + _ROUTE_W_VEC * e["vec"]
            + _ROUTE_W_BM25 * math.tanh(-float(e["best_bm25"]) / _ROUTE_BM25_SCALE),
            4,
        )
    ranked = sorted(entries.values(), key=lambda e: (-e["score"], e["kb_name"]))[:limit]
    result = {
        "query": query,
        "depth_req": req_orig,
        "depth_used": used,
        "degraded": degraded,
        "reason": reason,
        "reason_code": reason_code,
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
        "cached": False,
        "terms": terms,
        "kbs": ranked,
    }
    _route_cache_put(cache_key, result)
    return result


def _perm_params(user: str, admin: bool) -> dict[str, Any]:
    return {"user": user, "admin": 1 if admin else 0}


def _src_clause(
    sources: list[str] | None, params: dict[str, Any], prefix: str = "src", kb_name: str = ""
) -> str:
    """来源/知识库过滤片段，同时写入 params。"""
    clause = ""
    if sources:
        names = ",".join(f":{prefix}{i}" for i in range(len(sources)))
        params.update({f"{prefix}{i}": s for i, s in enumerate(sources)})
        clause += f" AND search_docs.source IN ({names})"
    if kb_name:
        clause += f" AND search_docs.kb_name = :{prefix}kb"
        params[f"{prefix}kb"] = kb_name
    return clause


def _vec_scores(
    conn, query: str, *, user: str, admin: bool, sources: list[str] | None, kb_name: str = ""
) -> tuple[dict[str, float], dict[str, Any]]:
    """语义召回（含阈值与权限过滤）：返回 ({doc_key: 相似度}, 信息)。"""
    try:
        from . import search_vec
    except Exception as exc:  # noqa: BLE001
        return {}, {"used": False, "model": "", "hits": 0, "reason": f"{type(exc).__name__}: {exc}"}
    if not search_vec.enabled():
        return {}, {"used": False, "model": "", "hits": 0, "reason": "已关闭"}
    hits = search_vec.search(conn, query, limit=60)
    if not hits:
        info = {"used": False, "model": search_vec.model(conn), "hits": 0}
        try:
            avail = search_vec.available(conn)
            if not avail.get("available"):
                info["reason"] = avail.get("reason", "语义不可用")
                info["need_download"] = bool(avail.get("need_download"))
            elif avail.get("stale"):
                info["reason"] = avail.get("reason", "向量需重新嵌入")
        except Exception:
            pass
        return {}, info
    top = hits[0][1]
    # 阈值按模型标定（bge-m3 与 zh-v1.5 的分数尺度差很大）
    floor = max(search_vec.vec_min(search_vec.model(conn)), top - _VEC_REL)
    kept = {k: s for k, s in hits if s >= floor}
    if not kept:
        return {}, {"used": False, "model": search_vec.model(), "hits": 0}
    keys = list(kept)
    params = _perm_params(user, admin)
    placeholders = ",".join(f":v{i}" for i in range(len(keys)))
    params.update({f"v{i}": k for i, k in enumerate(keys)})
    clause = _src_clause(sources, params, prefix="vsrc", kb_name=kb_name)
    allowed = {
        r["key"]
        for r in conn.execute(
            "SELECT note_path AS key FROM search_docs"
            f" WHERE note_path IN ({placeholders}) {_PERM_SQL}{clause}",
            params,
        )
    }
    final = {k: v for k, v in kept.items() if k in allowed}
    return final, {"used": bool(final), "model": search_vec.model(), "hits": len(final)}


def _vec_only_rows(
    conn, vmap: dict[str, float], have_keys: list[str], *, user: str, admin: bool,
    sources: list[str] | None, kb_name: str = "",
) -> list[dict[str, Any]]:
    """语义命中但词法未命中的文档行（rank 记 0），供融合成结果。"""
    keys = [k for k in vmap if k not in set(have_keys)]
    if not keys:
        return []
    params = _perm_params(user, admin)
    placeholders = ",".join(f":d{i}" for i in range(len(keys)))
    params.update({f"d{i}": k for i, k in enumerate(keys)})
    clause = _src_clause(sources, params, prefix="dsrc", kb_name=kb_name)
    out: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT search_docs.note_path AS key, search_docs.source AS source,"
        " search_docs.kb_name AS kb_name, search_docs.rel AS rel,"
        " search_docs.body AS body, search_docs.mtime AS mtime"
        f" FROM search_docs WHERE search_docs.note_path IN ({placeholders}) {_PERM_SQL}{clause}",
        params,
    ):
        out.append(
            {
                "key": r["key"],
                "source": r["source"],
                "kb_name": r["kb_name"],
                "rel": r["rel"],
                "body": r["body"],
                "mtime": r["mtime"],
                "rank": 0.0,
            }
        )
    return out


def _run_query(conn, match: str, params: dict[str, Any], src_clause: str, cand: int) -> tuple[int, list]:
    """按 FTS 表达式取总数 + 候选行（候选按 bm25 取，由调用方重排）。"""
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM search_fts JOIN search_docs ON search_docs.rowid = search_fts.rowid"
        f" WHERE search_fts MATCH :q {_PERM_SQL}{src_clause}",
        params,
    ).fetchone()["c"]
    if total == 0:
        return 0, []
    rows = conn.execute(
        "SELECT search_docs.note_path AS key, search_docs.source AS source,"
        " search_docs.kb_name AS kb_name, search_docs.rel AS rel,"
        " search_docs.body AS body, search_docs.mtime AS mtime,"
        " bm25(search_fts, 6.0, 1.0) AS rank"
        " FROM search_fts JOIN search_docs ON search_docs.rowid = search_fts.rowid"
        f" WHERE search_fts MATCH :q {_PERM_SQL}{src_clause}"
        " ORDER BY rank ASC LIMIT :cand",
        {**params, "cand": cand},
    ).fetchall()
    return total, rows
