# -*- coding: utf-8 -*-
"""笔记 / 知识库工作区全文搜索索引（SQLite FTS5 倒排表）

目标：像搜索引擎一样「查索引」，而不是每次请求把每篇文档读一遍。

索引源（两类）：
  - 个人笔记：`notepad_list` 下所有文件（source='note'，权限按归属/共享判定）
  - 知识库工作区：各知识库 `workspace` 目录下的文本文件（source='kb'，登录可见）

召回与排序：
  - 中文按二元切分（bigram）入索引（unicode61 不切汉字，整段汉字会变成一个 token）；
    英文/数字原样小写；写入前自行切分，读取用同一套规则切查询串。
  - 查询：多字中文段的各 bigram 之间 **OR 召回**（宽召回），空格分隔的多段之间 AND；
    `"引号"` 段转成 FTS5 短语（bigram 序列相邻）→ 精确匹配。
  - 排序：命中短语 > 覆盖率 > 词频/近邻度 > bm25（标题权重 6 / 正文 1），
    即「创建包」连写的文档排在只命中「创建」的文档前面，接近搜索引擎行为。

增量更新：
  - 服务内增删改经 file_store 变更通知即时标脏，下次查询补索引；
  - 桌面端直接落盘 / 托盘写入等外部改动由 TTL 全量比对（只 stat 比 mtime/size，
    内容没变不读文件）兜底。
"""
from __future__ import annotations

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
  )
"""

_hooks_installed = False
_lock = threading.Lock()
_refresh_lock = threading.Lock()
_pending: dict[str, str] = {}      # 笔记相对路径 -> 'upsert' | 'delete'
_last_scan: dict[str, float] = {}  # notes_root(str) -> monotonic

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


def _snippet(body: str, units: list[dict[str, str]], width: int = 180, lead: int = 60) -> tuple[str, list[str]]:
    """命中位置附近的摘要（短语优先定位）+ 高亮词列表。"""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "", []
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
) -> None:
    """写入/覆盖一篇文档（rowid 不变，FTS 行先删后插）。"""
    rel = rel or key
    now = datetime.now().isoformat(timespec="seconds")
    rowid = _rowid(conn, key)
    if rowid is None:
        cur = conn.execute(
            "INSERT INTO search_docs(note_path, source, kb_name, rel, title, body, size, mtime, indexed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (key, source, kb_name, rel, rel, body, size, mtime, now),
        )
        rowid = int(cur.lastrowid)
    else:
        conn.execute("DELETE FROM search_fts WHERE rowid = ?", (rowid,))
        conn.execute(
            "UPDATE search_docs SET source = ?, kb_name = ?, rel = ?, title = ?, body = ?,"
            " size = ?, mtime = ?, indexed_at = ? WHERE rowid = ?",
            (source, kb_name, rel, rel, body, size, mtime, now, rowid),
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


def _index_file(conn, root: Path, rel: str, size: int, mtime: float, *, source: str, kb_name: str) -> None:
    body = file_store.read_text_capped(file_store.resolve_note_path(root, rel), MAX_INDEX_BYTES)
    key = rel if source == "note" else _kb_key(kb_name, rel)
    _upsert(conn, key, body, size, mtime, source=source, kb_name=kb_name, rel=rel)


def _sources(conn, notes_root: Path) -> list[tuple[str, str, Path]]:
    """索引源列表：[(source, kb_name, root)]，含所有配置了工作区的知识库。"""
    sources: list[tuple[str, str, Path]] = [("note", "", Path(notes_root))]
    for name, ws in conn.execute(
        "SELECT name, workspace FROM knowledge_bases WHERE workspace <> '' ORDER BY name"
    ):
        root = Path(str(ws))
        if root.is_dir():
            sources.append(("kb", str(name), root))
    return sources


def _scan(conn, source: str, kb_name: str, root: Path, *, progress: bool = False) -> int:
    """全量比对 mtime/size，只重读变化过的文件；清理该来源下磁盘上已消失的索引行。

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
        if source == "kb" and p.suffix.lower() not in WORKSPACE_EXTS:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        key = rel if source == "note" else _kb_key(kb_name, rel)
        prev = known.pop(key, None)
        if prev is None or prev[0] != st.st_size or abs(prev[1] - st.st_mtime) >= 1e-6:
            try:
                _index_file(conn, root, rel, st.st_size, st.st_mtime, source=source, kb_name=kb_name)
            except (OSError, ValueError):
                pass
            else:
                touched += 1
        if progress:
            with _lock:
                _reindex_state["done"] = int(_reindex_state.get("done", 0)) + 1
    for key in known:
        _drop(conn, key)
        touched += 1
    return touched


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
            _index_file(conn, root, rel, st.st_size, st.st_mtime, source="note", kb_name="")
        except (OSError, ValueError):
            continue
        touched += 1
    # 2) TTL 全量比对（含知识库工作区）
    if due:
        for source, kb_name, src_root in _sources(conn, root):
            touched += _scan(conn, source, kb_name, src_root)
    if touched:
        conn.commit()
    return touched


def _rebuild(conn, notes_root: Path, *, progress: bool = False) -> int:
    """清空并全量重建索引（含知识库工作区）。"""
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
                )
        touched = 0
        with _lock:
            _reindex_state["phase"] = "indexing"
        for source, kb_name, root in _sources(conn, Path(notes_root)):
            touched += _scan(conn, source, kb_name, root, progress=progress)
            conn.commit()
        return touched


def reindex(conn, notes_root: Path) -> int:
    """同步重建全部索引（含知识库工作区），返回写入/删除的索引行数。"""
    return _rebuild(conn, notes_root)


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
    try:
        updated = _rebuild(conn, notes_root, progress=True)
        with _lock:
            _reindex_state["updated"] = updated
    except Exception as exc:  # noqa: BLE001 - 状态页展示错误即可
        with _lock:
            _reindex_state["error"] = f"{type(exc).__name__}: {exc}"
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


def stats(conn, notes_root: Path, *, deep: bool = False) -> dict[str, Any]:
    """索引状态：总量、分来源明细、增量队列、后台重建进度。

    `deep=True` 时额外做磁盘校对（统计磁盘文件数 / 索引缺失 / 版本过期）与 FTS 完整性检查。
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
            indexed = {
                r["rel"]: (int(r["size"]), float(r["mtime"]))
                for r in conn.execute(
                    "SELECT rel, size, mtime FROM search_docs WHERE source = ? AND kb_name = ?",
                    (source, kb_name),
                )
            }
            disk = 0
            missing = 0
            changed = 0
            seen: set[str] = set()
            for p in src_root.rglob("*"):
                if not p.is_file():
                    continue
                if source == "kb" and p.suffix.lower() not in WORKSPACE_EXTS:
                    continue
                disk += 1
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = p.relative_to(src_root).as_posix()
                seen.add(rel)
                prev = indexed.get(rel)
                if prev is None:
                    missing += 1
                elif prev[0] != st.st_size or abs(prev[1] - st.st_mtime) >= 1e-6:
                    changed += 1
            extra = len([k for k in indexed if k not in seen])
            item.update({"disk_files": disk, "missing": missing, "changed": changed, "extra": extra})
        sources.append(item)

    result: dict[str, Any] = {
        "docs": int(docs),
        "vec": _vec_stats(conn),
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
        "reindex": reindex_state(),
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
    "MAX_INDEX_BYTES",
    "WORKSPACE_EXTS",
]


# ── 检索 ────────────────────────────────────────────────


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
) -> dict[str, Any]:
    """倒排检索（只查索引表，不读文档）。

    宽召回 + 重排：同段 bigram OR 召回，再按「短语命中 > 覆盖率 > 词频/近邻 > bm25」加权评分排序。
    `"引号"` 精确短语无结果时自动回退为整串模糊匹配（`fallback=True`）。
    返回 {total, hits, took_ms, fallback}；hit 含 source（note/kb）、kb_name、rel、path、
    snippet、coverage、tf、proximity、phrase_hits、bm25、score。
    """
    empty: dict[str, Any] = {"total": 0, "hits": [], "took_ms": 0.0, "fallback": False,
                             "vec": {"used": False, "model": "", "hits": 0}}
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
            return {**empty, "took_ms": round((time.perf_counter() - started) * 1000, 2), "vec": vinfo}
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
        return {**empty, "took_ms": round((time.perf_counter() - started) * 1000, 2)}
    total = len(vmap) if mode == "sem" and vmap else max(total, len(rows))

    hits: list[dict[str, Any]] = []
    for r in rows:
        meta = _score((r["body"] or "").lower(), units, float(r["rank"]))
        vec = vmap.get(r["key"], 0.0)
        if vec:
            meta["vec"] = round(vec, 4)
            meta["score"] = round(vec if mode == "sem" else meta["score"] + _W_VEC * vec, 4)
        snippet, matches = _snippet(r["body"], units)
        hits.append(
            {
                "path": r["rel"],
                "source": r["source"],
                "kb_name": r["kb_name"],
                "rel": r["rel"],
                "snippet": snippet,
                "matches": matches,
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
    page = hits[offset : offset + limit]
    return {
        "total": total,
        "hits": page,
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
        "fallback": fallback,
        "vec": vinfo,
    }


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
