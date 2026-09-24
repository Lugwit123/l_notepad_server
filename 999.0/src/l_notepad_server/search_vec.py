# -*- coding: utf-8 -*-
"""笔记 / 知识库归档的向量语义检索（本机 Ollama embedding + SQLite 存向量）

设计（不引入第三方依赖，服务端只有标准库）：
- 分块：按空行/标题切段，超长再硬切，块间留少量重叠；
- 嵌入：调用本机 Ollama HTTP 接口（`/api/embed` 批量，失败回退 `/api/embeddings` 单个）；
- 存储：`vec_chunks` 存块文本 + 归一化后的 float32 向量（BLOB），`vec_docs` 记录
  来源 size/mtime（笔记）/ rev（知识库归档）用于增量重嵌；
- 内容来源：个人笔记读本机文件，知识库读 depot 已上传归档（不依赖本机工作区）；
- 检索：查询串嵌入后与内存缓存的块向量做点积（向量已归一化 → 点积即余弦），
  取每篇文档最高分块分作为该文档的语义相似度；
- 与词法检索的融合在 `search_index.search` 里做（`_W_VEC` 加权）。

配置（环境变量）：
  L_NOTEPAD_EMBED_URL   默认 http://127.0.0.1:11434
  L_NOTEPAD_EMBED_MODEL 默认自动挑（优先 bge-m3，其次 nomic-embed-text）
  L_NOTEPAD_VEC_ENABLED 默认 1；置 0 关闭语义检索
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from array import array
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import search_index

EMBED_URL = os.environ.get("L_NOTEPAD_EMBED_URL", "http://127.0.0.1:11434").rstrip("/")
EMBED_MODEL_ENV = os.environ.get("L_NOTEPAD_EMBED_MODEL", "").strip()
PREFERRED_MODELS = ("bge-m3", "bge-base-zh-v1.5", "bge-small-zh-v1.5", "nomic-embed-text")

# 可选模型目录（Ollama 仓库名 → 展示信息）。切换模型前需已安装，未安装时前端提示下载。
MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "name": "bge-m3",
        "label": "BGE-M3（多语言）",
        "dim": 1024,
        "ctx": 8192,
        "size_mb": 1200,
        "langs": "中英 + 100 语言",
        "note": "质量最好；长文 8192 token，4G 内存机器偏重",
    },
    {
        "name": "quentinz/bge-base-zh-v1.5",
        "label": "BGE-base-zh-v1.5（中文）",
        "dim": 768,
        "ctx": 512,
        "size_mb": 205,
        "langs": "中文（英文较弱）",
        "note": "省内存首选；512 token，需把分块降到 400 字符左右",
    },
    {
        "name": "qllama/bge-small-zh-v1.5",
        "label": "BGE-small-zh-v1.5（超轻）",
        "dim": 512,
        "ctx": 512,
        "size_mb": 26,
        "langs": "中文",
        "note": "极省资源（2C4G 服务器可用）；质量略低于 base",
    },
    {
        "name": "nomic-embed-text",
        "label": "nomic-embed-text（英文）",
        "dim": 768,
        "ctx": 2048,
        "size_mb": 274,
        "langs": "英文为主",
        "note": "中文检索质量差，仅作兼容保留",
    },
]

# 用户在状态页选择的模型（持久化在 app_settings.embed_model）
SETTING_EMBED_MODEL = "embed_model"

CHUNK_SIZE = 900        # 目标块大小（字符，上限）
CHUNK_OVERLAP = 120
BATCH = 16              # 每次批量嵌入的块数

# v1.5 系（bge-*-zh-v1.5）官方要求：短查询加 instruction 前缀，文档不加（对称检索）。
# bge-m3 / nomic / small-zh 不需要。
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def chunk_size_for(model_name: str = "") -> int:
    """按模型上下文自动取块大小（字符）：留约 20% 余量给 tokenizer。

    bge-m3 ctx 8192 → 保持 900；v1.5 系 ctx 512 → 400（否则尾部会被截断丢弃）。
    """
    name = (model_name or "").lower()
    ctx = next((m["ctx"] for m in MODEL_CATALOG if m["name"].lower() == name), 0)
    if not ctx:
        return CHUNK_SIZE
    return max(200, min(CHUNK_SIZE, int(ctx * 0.8)))


def needs_query_instruction(model_name: str = "") -> bool:
    name = (model_name or "").lower()
    return "zh-v1.5" in name or "zh-1.5" in name


# 语义相似度下限按模型标定（不同模型分数尺度差异极大，实测本语料）：
#   bge-m3        : 负例 0.43-0.52 / 正例 0.65-0.70 → 0.55
#   bge-base-zh   : 负例 0.23-0.29 / 正例 0.51-0.67 → 0.35
VEC_MIN_DEFAULT = 0.45


def vec_min(model_name: str = "") -> float:
    """当前模型建议的相似度下限（低于此值视为不相关）。"""
    name = (model_name or "").lower()
    if "bge-m3" in name:
        return 0.55
    if "zh-v1.5" in name or "zh-1.5" in name:
        return 0.35
    return VEC_MIN_DEFAULT


def query_text(query: str, model_name: str = "") -> str:
    """给查询加上模型需要的 instruction 前缀（文档侧不加）。"""
    if needs_query_instruction(model_name):
        return QUERY_INSTRUCTION + (query or "")
    return query or ""
MAX_CHUNKS_PER_DOC = 200  # 超长文档的块数上限
CACHE_MAX_CHUNKS = 20000  # 内存缓存的块数上限（超出则只缓存最近使用的文档）

_lock = threading.Lock()
_cache: dict[str, list[tuple[int, tuple[float, ...]]]] = {}  # doc_key -> [(chunk_no, vec)]
_cache_dirty = True
_model_cache: str = ""

# 后台嵌入任务状态（状态页展示）
embed_state: dict[str, Any] = {
    "running": False,
    "phase": "",       # probing / embedding
    "total": 0,        # 待嵌入块数
    "done": 0,
    "docs": 0,         # 本次处理文档数
    "model": "",
    "started_at": "",
    "finished_at": "",
    "error": "",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 模型下载状态（仅在用户明确确认下载后才会启动）
download_state: dict[str, Any] = {
    "running": False,
    "model": "",
    "status": "",
    "completed": 0,
    "total": 0,
    "error": "",
    "started_at": "",
    "finished_at": "",
}


def init_settings_schema(conn) -> None:
    """通用 KV 设置表（目前存所选 embedding 模型）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()


def get_setting(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings(key, value) VALUES(?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def enabled() -> bool:
    return os.environ.get("L_NOTEPAD_VEC_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


# ── Ollama 交互 ─────────────────────────────────────────


def _get_json(path: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(f"{EMBED_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _post_json(path: str, payload: dict, timeout: float = 180.0) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{EMBED_URL}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def installed_names() -> set[str]:
    """Ollama 里已安装的模型名（含去掉命名空间后的短名，便于匹配）。"""
    out: set[str] = set()
    try:
        for m in _get_json("/api/tags").get("models", []):
            name = str(m.get("name") or m.get("model") or "").strip()
            if not name:
                continue
            out.add(name.lower())
            out.add(name.split(":")[0].lower())
            out.add(name.split("/")[-1].split(":")[0].lower())
    except Exception:
        pass
    return out


def catalog(conn=None) -> list[dict[str, Any]]:
    """可选模型列表（含是否已安装、是否当前使用中）。"""
    have = installed_names()
    cur = model(conn)
    items: list[dict[str, Any]] = []
    for spec in MODEL_CATALOG:
        short = spec["name"].split("/")[-1].split(":")[0].lower()
        items.append({**spec, "installed": short in have, "current": spec["name"] == cur})
    return items


def model(conn=None) -> str:
    """当前 embedding 模型：环境变量 > 页面设置 > 自动挑（按 PREFERRED_MODELS 顺序）。"""
    global _model_cache
    if EMBED_MODEL_ENV:
        return EMBED_MODEL_ENV
    if conn is not None:
        try:
            configured = get_setting(conn, SETTING_EMBED_MODEL)
        except Exception:
            configured = ""
        if configured:
            return configured
    if _model_cache:
        return _model_cache
    names = installed_names()
    for pref in PREFERRED_MODELS:
        if pref.lower() in names:
            _model_cache = pref
            return pref
    return sorted(names)[0] if names else ""


def available(conn=None) -> dict[str, Any]:
    """语义检索可用性（供状态页/接口展示）。"""
    if not enabled():
        return {"available": False, "reason": "已通过 L_NOTEPAD_VEC_ENABLED=0 关闭"}
    mdl = model(conn)
    if not mdl:
        return {"available": False, "reason": f"未探测到 embedding 模型（{EMBED_URL}）"}
    short = mdl.split("/")[-1].split(":")[0].lower()
    if short not in installed_names():
        return {"available": False, "model": mdl, "reason": f"模型未安装：{mdl}（需先下载）", "need_download": True}
    info: dict[str, Any] = {"available": True, "model": mdl, "url": EMBED_URL}
    if conn is not None:
        try:
            docs = conn.execute("SELECT COUNT(*) AS c FROM vec_docs").fetchone()["c"]
            cur = conn.execute("SELECT COUNT(*) AS c FROM vec_docs WHERE model = ?", (mdl,)).fetchone()["c"]
        except Exception:
            docs, cur = 0, 0
        info["docs"] = int(docs)
        if docs and not cur:
            info["stale"] = True
            info["reason"] = f"已切换模型为 {mdl}，向量需要重新嵌入"
    return info


def set_model(conn, name: str) -> dict[str, Any]:
    """切换 embedding 模型（写设置 + 清向量缓存）。

    未安装时不下载，只返回 need_download=True 让调用方提示用户。
    """
    global _model_cache
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "模型名不能为空"}
    short = name.split("/")[-1].split(":")[0].lower()
    installed = short in installed_names()
    if not installed:
        return {"ok": False, "need_download": True, "model": name,
                "error": f"模型未安装：{name}（需要先下载）"}
    set_setting(conn, SETTING_EMBED_MODEL, name)
    _model_cache = name
    reset_cache()
    try:
        stale = [r["doc_key"] for r in conn.execute("SELECT doc_key FROM vec_docs WHERE model <> ?", (name,))]
        for key in stale:
            conn.execute("DELETE FROM vec_chunks WHERE doc_key = ?", (key,))
        conn.execute("DELETE FROM vec_docs WHERE model <> ?", (name,))
        conn.commit()
    except Exception:
        pass
    return {"ok": True, "model": name, "need_embed": True}


def start_download(name: str) -> bool:
    """后台下载模型（只有显式调用才下载，不会自动触发）。"""
    name = (name or "").strip()
    with _lock:
        if download_state.get("running") or not name:
            return False
        download_state.update({
            "running": True, "model": name, "status": "starting", "completed": 0,
            "total": 0, "error": "", "started_at": _now(), "finished_at": "",
        })
    thread = threading.Thread(target=_download_worker, args=(name,), name="embed_pull", daemon=True)
    thread.start()
    return True


def _download_worker(name: str) -> None:
    global _model_cache
    body = json.dumps({"name": name, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{EMBED_URL}/api/pull", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with _lock:
                    download_state["status"] = str(msg.get("status") or download_state["status"])
                    if msg.get("total"):
                        download_state["total"] = int(msg["total"])
                        download_state["completed"] = int(msg.get("completed") or 0)
                    if msg.get("error"):
                        download_state["error"] = str(msg["error"])
    except Exception as exc:  # noqa: BLE001
        with _lock:
            download_state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ok = not download_state.get("error")
        with _lock:
            download_state["running"] = False
            download_state["finished_at"] = _now()
            if ok:
                download_state["status"] = "success"
                _model_cache = name  # 下载完成即视为可用
        if ok:
            # 拉完自动切换为当前模型（用户点过"下载"即表示要用它）
            try:
                from . import db as dbmod  # 延迟导入，避免循环

                conn = dbmod.connect(_db_path_for_settings())
                set_setting(conn, SETTING_EMBED_MODEL, name)
                conn.close()
                reset_cache()
            except Exception:
                pass


_db_for_settings: Path | None = None


def bind_db_path(db_path: Path) -> None:
    """记录数据库路径（下载完成后自动写设置用）。"""
    global _db_for_settings
    _db_for_settings = Path(db_path)


def _db_path_for_settings() -> Path:
    if _db_for_settings is not None:
        return _db_for_settings
    from . import db as dbmod

    return dbmod.default_db_path()


def model_status(conn) -> dict[str, Any]:
    """模型选择区用的状态：目录 + 当前模型 + 下载进度。"""
    with _lock:
        dl = dict(download_state)
    return {
        "catalog": catalog(conn),
        "current": model(conn),
        "env_override": EMBED_MODEL_ENV or "",
        "available": available(conn),
        "download": dl,
        "url": EMBED_URL,
    }


def embed_texts(texts: list[str], model_name: str = "") -> list[list[float]]:
    """批量嵌入；`/api/embed` 不可用时回退逐条 `/api/embeddings`。"""
    if not texts:
        return []
    mdl = model_name or model()
    try:
        data = _post_json("/api/embed", {"model": mdl, "input": list(texts)})
        vecs = data.get("embeddings")
        if isinstance(vecs, list) and len(vecs) == len(texts):
            return [[float(x) for x in v] for v in vecs]
    except Exception:
        pass
    out: list[list[float]] = []
    for t in texts:
        data = _post_json("/api/embeddings", {"model": mdl, "prompt": t})
        vec = data.get("embedding") or []
        if not vec:
            raise RuntimeError(f"embedding 返回为空（model={mdl}）")
        out.append([float(x) for x in vec])
    return out


# ── 向量编解码 / 相似度 ──────────────────────────────────


def _normalize(vec: list[float]) -> tuple[float, ...]:
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return tuple(v / norm for v in vec)


def _pack(vec: Iterable[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> tuple[float, ...]:
    arr = array("f")
    arr.frombytes(blob)
    return tuple(arr)


def _dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b))


# ── 分块 ────────────────────────────────────────────────


def chunk_spans(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, str]]:
    """按空行/标题切成段，超长段落再按 size 硬切并保留 overlap 重叠。

    返回 [(块在归一化正文中的起始偏移, 块文本)]；偏移取该块首个段落的起始位置
    （段间多余空行会被合并成 `\\n\\n`，块文本与正文切片可能有细微差异，定位时再按文本查找兜底）。
    """
    norm = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not norm:
        return []
    spans: list[tuple[int, str]] = []
    buf = ""
    buf_start = 0
    pos, total = 0, len(norm)
    while pos <= total:
        sep = norm.find("\n\n", pos)
        end = total if sep < 0 else sep
        raw = norm[pos:end]
        para = raw.strip()
        p_start = pos + (len(raw) - len(raw.lstrip()))
        pos = total + 1 if sep < 0 else sep + 2
        if not para:
            continue
        if len(buf) + len(para) + 2 <= size:
            if not buf:
                buf_start = p_start
            buf = f"{buf}\n\n{para}" if buf else para
            continue
        if buf:
            spans.append((buf_start, buf))
            buf = ""
        if len(para) <= size:
            buf, buf_start = para, p_start
            continue
        step = max(1, size - overlap)
        for i in range(0, len(para), step):
            piece = para[i : i + size]
            if piece.strip():
                spans.append((p_start + i, piece))
            if i + size >= len(para):
                break
    if buf:
        spans.append((buf_start, buf))
    return spans[:MAX_CHUNKS_PER_DOC]


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按空行/标题切成段，超长段落再按 size 硬切并保留 overlap 重叠。"""
    return [t for _start, t in chunk_spans(text, size=size, overlap=overlap)]


# ── 建表 / 增量嵌入 ─────────────────────────────────────


def init_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vec_docs (
          doc_key TEXT PRIMARY KEY,
          source TEXT NOT NULL DEFAULT 'note',
          kb_name TEXT NOT NULL DEFAULT '',
          rel TEXT NOT NULL DEFAULT '',
          size INTEGER NOT NULL DEFAULT 0,
          mtime REAL NOT NULL DEFAULT 0,
          rev INTEGER NOT NULL DEFAULT 0,
          chunks INTEGER NOT NULL DEFAULT 0,
          model TEXT NOT NULL DEFAULT '',
          embedded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vec_chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doc_key TEXT NOT NULL,
          chunk_no INTEGER NOT NULL,
          text TEXT NOT NULL,
          "start" INTEGER NOT NULL DEFAULT 0,
          dim INTEGER NOT NULL DEFAULT 0,
          vec BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vec_chunks_doc ON vec_chunks(doc_key);
        -- 库摘要向量（选库路由 depth=2 用）：库名 → 摘要向量，避免每次扫全部块
        CREATE TABLE IF NOT EXISTS kb_vec (
          kb_name TEXT PRIMARY KEY,
          model TEXT NOT NULL DEFAULT '',
          dim INTEGER NOT NULL DEFAULT 0,
          text TEXT NOT NULL DEFAULT '',
          vec BLOB NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    _ensure_column(conn, "vec_docs", "rev", "rev INTEGER NOT NULL DEFAULT 0")
    # 块在正文中的起始偏移（旧库补列，默认 0 → 定位时回退到按块文本查找）
    _ensure_column(conn, "vec_chunks", "start", '"start" INTEGER NOT NULL DEFAULT 0')
    init_settings_schema(conn)
    conn.commit()


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """幂等补列（旧库升级：知识库源改走 depot 归档后需要 rev 做增量比对）。

    PRAGMA table_info 第 0 列是 cid，列名在第 1 列（不依赖 row_factory）。
    """
    cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _doc_key(source: str, kb_name: str, rel: str) -> str:
    return rel if source == "note" else search_index._kb_key(kb_name, rel)


def _pending_docs(conn, notes_root: Path, force: bool = False) -> list[dict[str, Any]]:
    """待嵌入文档：[{key, source, kb_name, rel, size, mtime, rev, path}]。

    以 search_docs（词法索引）为准，比对 vec_docs 的 size/mtime/rev 找出新增/变更；
    同时清理 vec_docs 中已不在 search_docs 的条目。个人笔记读本机文件（path），
    知识库文档读 depot 归档（path 为 None，内容按 rel+rev 取）。
    """
    docs_rows = conn.execute(
        "SELECT note_path AS key, source, kb_name, rel, size, mtime, rev FROM search_docs"
    ).fetchall()
    have = {
        r["doc_key"]: (int(r["size"]), float(r["mtime"]), int(r["rev"]), str(r["model"]))
        for r in conn.execute("SELECT doc_key, size, mtime, rev, model FROM vec_docs")
    }
    mdl = model(conn)
    todo: list[dict[str, Any]] = []
    alive: set[str] = set()
    for r in docs_rows:
        key = str(r["key"])
        alive.add(key)
        if not force and have.get(key) == (int(r["size"]), float(r["mtime"]), int(r["rev"]), mdl):
            continue
        source, kb_name, rel = str(r["source"]), str(r["kb_name"]), str(r["rel"])
        path: Path | None = None
        if source == "note":
            try:
                path = search_index.file_store.resolve_note_path(Path(notes_root), rel)
            except ValueError:
                continue
        todo.append({
            "key": key, "source": source, "kb_name": kb_name, "rel": rel,
            "size": int(r["size"]), "mtime": float(r["mtime"]), "rev": int(r["rev"]),
            "path": path,
        })
    stale = [k for k in have if k not in alive]
    for key in stale:
        drop_doc(conn, key)
    if stale:
        conn.commit()
    return todo


def _doc_text(conn, doc: dict[str, Any]) -> str:
    """取待嵌入正文：个人笔记读本机文件，知识库文档读 depot 归档（不依赖本机工作区）。"""
    if doc["source"] == "note":
        return search_index.file_store.read_text_capped(doc["path"], search_index.MAX_INDEX_BYTES)
    from . import depot_map

    return depot_map.read_text(
        conn, doc["kb_name"], rel=doc["rel"], rev=int(doc["rev"]),
        max_bytes=search_index.MAX_INDEX_BYTES,
    )


def drop_doc(conn, doc_key: str) -> None:
    conn.execute("DELETE FROM vec_chunks WHERE doc_key = ?", (doc_key,))
    conn.execute("DELETE FROM vec_docs WHERE doc_key = ?", (doc_key,))
    _invalidate()


def embed_doc(conn, doc: dict[str, Any]) -> int:
    """嵌入一篇文档（先删旧块）。返回块数。"""
    key, source, kb_name, rel = doc["key"], doc["source"], doc["kb_name"], doc["rel"]
    model_name = model(conn)
    text = _doc_text(conn, doc)
    block = chunk_size_for(model_name)
    chunks = chunk_spans(text, size=block, overlap=max(40, block // 8))
    conn.execute("DELETE FROM vec_chunks WHERE doc_key = ?", (key,))
    if not chunks:
        conn.execute("DELETE FROM vec_docs WHERE doc_key = ?", (key,))
        _invalidate()
        return 0
    model_name = model(conn)
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i : i + BATCH]
        vecs = embed_texts([t for _s, t in batch], model_name)
        for j, ((start, chunk), vec) in enumerate(zip(batch, vecs)):
            norm = _normalize(vec)
            conn.execute(
                'INSERT INTO vec_chunks(doc_key, chunk_no, text, "start", dim, vec) VALUES(?,?,?,?,?,?)',
                (key, i + j, chunk, int(start), len(norm), _pack(norm)),
            )
        with _lock:
            embed_state["done"] = int(embed_state.get("done", 0)) + len(batch)
    conn.execute(
        "INSERT INTO vec_docs(doc_key, source, kb_name, rel, size, mtime, rev, chunks, model, embedded_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_key) DO UPDATE SET source=excluded.source, kb_name=excluded.kb_name,"
        " rel=excluded.rel, size=excluded.size, mtime=excluded.mtime, rev=excluded.rev,"
        " chunks=excluded.chunks, model=excluded.model, embedded_at=excluded.embedded_at",
        (key, source, kb_name, rel, int(doc["size"]), float(doc["mtime"]), int(doc["rev"]),
         len(chunks), model_name, _now()),
    )
    conn.commit()
    _invalidate()
    return len(chunks)


def refresh(conn, notes_root: Path, *, force: bool = False) -> int:
    """增量嵌入（同步）。返回本次嵌入的文档数。"""
    if not enabled() or not model():
        return 0
    todo = _pending_docs(conn, notes_root, force=force)
    if not todo:
        return 0
    done = 0
    for doc in todo:
        try:
            embed_doc(conn, doc)
            done += 1
        except Exception as exc:  # noqa: BLE001 - 单篇失败不影响其它
            with _lock:
                embed_state["error"] = f"{type(exc).__name__}: {exc}"
            break
    return done


def start_embed_async(db_path: Path, notes_root: Path, *, force: bool = False) -> bool:
    """后台线程增量嵌入（状态页用）。"""
    with _lock:
        if embed_state.get("running"):
            return False
        embed_state.update({
            "running": True, "phase": "probing", "total": 0, "done": 0, "docs": 0,
            "model": model(), "started_at": _now(), "finished_at": "", "error": "",
        })
    thread = threading.Thread(
        target=_embed_worker, args=(Path(db_path), Path(notes_root), force), name="search_embed", daemon=True
    )
    thread.start()
    return True


def _embed_worker(db_path: Path, notes_root: Path, force: bool) -> None:
    from . import db as dbmod

    conn = dbmod.connect(db_path)
    try:
        todo = _pending_docs(conn, notes_root, force=force)
        with _lock:
            embed_state["phase"] = "embedding"
            embed_state["total"] = len(todo)
        for doc in todo:
            embed_doc(conn, doc)
            with _lock:
                embed_state["docs"] = int(embed_state.get("docs", 0)) + 1
    except Exception as exc:  # noqa: BLE001
        with _lock:
            embed_state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            conn.close()
        except Exception:
            pass
        with _lock:
            embed_state["running"] = False
            embed_state["finished_at"] = _now()


# ── 检索 ────────────────────────────────────────────────


def _invalidate() -> None:
    global _cache_dirty
    with _lock:
        _cache_dirty = True


def reset_cache() -> None:
    """清空内存向量缓存（切换模型 / 重新嵌入后调用）。"""
    global _cache, _cache_dirty
    with _lock:
        _cache = {}
        _cache_dirty = True


def _load_cache(conn) -> dict[str, list[tuple[int, tuple[float, ...]]]]:
    """把块向量读进内存（首次或索引变更后重建）。"""
    global _cache, _cache_dirty
    with _lock:
        if not _cache_dirty and _cache:
            return _cache
    cache: dict[str, list[tuple[int, tuple[float, ...]]]] = {}
    total = 0
    for r in conn.execute(
        "SELECT doc_key, chunk_no, vec FROM vec_chunks ORDER BY id DESC LIMIT ?", (CACHE_MAX_CHUNKS,)
    ):
        cache.setdefault(r["doc_key"], []).append((int(r["chunk_no"]), _unpack(r["vec"])))
        total += 1
    with _lock:
        _cache = cache
        _cache_dirty = False
    return cache


def _best_chunks(conn, query: str, *, limit: int) -> list[tuple[str, int, float]]:
    """每篇文档取相似度最高的块：[(doc_key, chunk_no, 相似度)]，按相似度降序。"""
    if not enabled() or not query.strip():
        return []
    mdl = model(conn)
    if not mdl:
        return []
    cache = _load_cache(conn)
    if not cache:
        return []
    # 当前模型的向量不存在（刚切换模型）→ 语义暂不可用，需重新嵌入
    row = conn.execute("SELECT COUNT(*) AS c FROM vec_docs").fetchone()
    if row and int(row["c"]) and not conn.execute(
        "SELECT 1 FROM vec_docs WHERE model = ? LIMIT 1", (mdl,)
    ).fetchone():
        return []
    try:
        qvec = _normalize(embed_texts([query_text(query, mdl)], mdl)[0])
    except Exception:
        return []
    best: dict[str, tuple[int, float]] = {}
    for doc_key, chunks in cache.items():
        top_no, top = 0, 0.0
        for no, vec in chunks:
            score = _dot(qvec, vec)
            if score > top:
                top, top_no = score, no
        if top > 0:
            best[doc_key] = (top_no, top)
    ranked = sorted(best.items(), key=lambda kv: kv[1][1], reverse=True)[:limit]
    return [(k, no, score) for k, (no, score) in ranked]


def search(conn, query: str, *, limit: int = 50) -> list[tuple[str, float]]:
    """语义检索：返回 [(doc_key, 相似度)]，按相似度降序（每篇取最高分块）。"""
    return [(k, score) for k, _no, score in _best_chunks(conn, query, limit=limit)]


def _probe_embed(timeout: float = 0.3) -> bool:
    """快速 TCP 探测 embedding 服务是否可达。

    选库路由 depth=2 必须快：ollama 不可达时 `embed_texts` 会卡在 HTTP 长超时
    （这也是 hybrid 无 ollama 时约 6s 的原因）。先做 0.3s 建连探测，不可达立即放弃。
    """
    try:
        from urllib.parse import urlparse

        u = urlparse(EMBED_URL)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001 不可达 / DNS / 超时一律视为不可用
        return False


def _kb_meta_texts(conn) -> list[tuple[str, str]]:
    """[(kb_name, meta_text)]：name + title + description（库摘要文本）。"""
    try:
        from . import knowledge

        bases = knowledge.list_bases(conn)
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, str]] = []
    for b in bases:
        name = str(b.get("name") or "")
        text = " ".join(
            (name, str(b.get("title") or ""), str(b.get("description") or ""))
        ).strip()
        out.append((name, text or name))
    return out


def _kb_doc_centroid(conn, kb_name: str, mdl: str) -> tuple[float, ...] | None:
    """该库已嵌入文档的块向量均值（归一化）；无则 None。"""
    rows = conn.execute(
        "SELECT c.vec AS vec FROM vec_chunks c JOIN vec_docs d ON d.doc_key = c.doc_key"
        " WHERE d.kb_name = ? AND d.model = ?",
        (kb_name, mdl),
    ).fetchall()
    if not rows:
        return None
    acc: list[float] | None = None
    for r in rows:
        v = _unpack(r["vec"])
        if acc is None:
            acc = [0.0] * len(v)
        for i, x in enumerate(v):
            acc[i] += x
    n = len(rows)
    return _normalize([x / n for x in acc])


def _kb_vec_load(conn, kb_name: str, mdl: str) -> tuple[float, ...] | None:
    row = conn.execute(
        "SELECT vec FROM kb_vec WHERE kb_name = ? AND model = ?", (kb_name, mdl)
    ).fetchone()
    return _unpack(row["vec"]) if row else None


def _kb_vec_build(conn, kb_name: str, meta_text: str, mdl: str) -> tuple[float, ...] | None:
    """库摘要向量 = 归一化(0.5*元数据向量 + 0.5*文档质心)；缺一用另一；都无 → None。"""
    try:
        meta_vec: tuple[float, ...] | None = _normalize(embed_texts([meta_text], mdl)[0])
    except Exception:  # noqa: BLE001
        meta_vec = None
    centroid = _kb_doc_centroid(conn, kb_name, mdl)
    if meta_vec and centroid:
        vec = _normalize([0.5 * a + 0.5 * b for a, b in zip(meta_vec, centroid)])
    else:
        vec = meta_vec or centroid
    if vec is None:
        return None
    try:
        conn.execute(
            "INSERT OR REPLACE INTO kb_vec(kb_name, model, dim, text, vec, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (kb_name, mdl, len(vec), meta_text, _pack(vec), _now()),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 缓存写失败不影响本次返回
        pass
    return vec


def _kb_vec_ensure(conn, kb_name: str, meta_text: str, mdl: str) -> tuple[float, ...] | None:
    vec = _kb_vec_load(conn, kb_name, mdl)
    if vec is not None:
        return vec
    return _kb_vec_build(conn, kb_name, meta_text, mdl)


def kb_semantic_scores(
    conn, query: str, *, probe_s: float = 0.3, max_kbs: int = 200
) -> dict[str, float]:
    """按**知识库**聚合的语义得分：{kb_name: 需求与该库摘要向量余弦}。

    查询只嵌一次，再与各库**预建摘要向量**点积（O(库数)，不扫全部块）。
    不可用 / 不可达 / 无向量时返回 `{}`（**绝不阻塞**，供选库路由 depth=2 降级）。
    """
    if not enabled() or not (query or "").strip():
        return {}
    mdl = model(conn)
    if not mdl:
        return {}
    if not _probe_embed(probe_s):
        return {}
    try:
        qvec = _normalize(embed_texts([query_text(query, mdl)], mdl)[0])
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for kb_name, meta_text in _kb_meta_texts(conn)[:max_kbs]:
        vec = _kb_vec_ensure(conn, kb_name, meta_text, mdl)
        if vec is None:
            continue
        score = _dot(qvec, vec)
        if score > 0:
            out[kb_name] = round(score, 4)
    return out


def search_chunks(conn, query: str, *, limit: int = 50) -> list[tuple[str, int, int, str, float]]:
    """语义检索（块级）：[(doc_key, chunk_no, 起始偏移, 块文本, 相似度)]，降序（每篇取最高分块）。"""
    out: list[tuple[str, int, int, str, float]] = []
    for doc_key, chunk_no, score in _best_chunks(conn, query, limit=limit):
        row = conn.execute(
            'SELECT "start" AS start, text FROM vec_chunks WHERE doc_key = ? AND chunk_no = ?',
            (doc_key, chunk_no),
        ).fetchone()
        if row is None:
            continue
        out.append((doc_key, chunk_no, int(row["start"]), str(row["text"]), score))
    return out


def doc_chunks(conn, doc_keys: Iterable[str]) -> dict[str, list[tuple[int, int, str]]]:
    """批量取文档全部块：{doc_key: [(chunk_no, 起始偏移, 块文本)]}（块序升序）。"""
    keys = [k for k in dict.fromkeys(doc_keys or []) if k]
    if not keys:
        return {}
    out: dict[str, list[tuple[int, int, str]]] = {}
    for i in range(0, len(keys), 200):
        batch = keys[i : i + 200]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            'SELECT doc_key, chunk_no, "start" AS start, text FROM vec_chunks'
            f" WHERE doc_key IN ({placeholders}) ORDER BY doc_key, chunk_no",
            tuple(batch),
        )
        for r in rows:
            out.setdefault(str(r["doc_key"]), []).append(
                (int(r["chunk_no"]), int(r["start"]), str(r["text"]))
            )
    return out


# ── 重排（本地 cross-encoder，llama.cpp /rerank）─────────
#
# 与 embedding 一样走 HTTP + 标准库 urllib，服务端不引第三方依赖。
# 未配置地址 / 关闭 / 冷却 / 调用失败一律降级：调用方退回原融合排序。


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


RERANK_URL = os.environ.get("L_NOTEPAD_RERANK_URL", "").strip().rstrip("/")
RERANK_MODEL_ENV = os.environ.get("L_NOTEPAD_RERANK_MODEL", "").strip()
RERANK_TIMEOUT_S = _env_float("L_NOTEPAD_RERANK_TIMEOUT_S", 3.0)
RERANK_TOP_N = _env_int("L_NOTEPAD_RERANK_TOP_N", 40)
RERANK_FAIL_LIMIT = 3       # 连续失败多少次后进入冷却
RERANK_COOLDOWN_S = 60.0    # 冷却时长（冷却期内不发起请求）
RERANK_PROBE_TTL_S = 60.0   # 可用性探测结果的缓存时长

SETTING_RERANK_ENABLED = "rerank_enabled"
SETTING_RERANK_MODEL = "rerank_model"

# 重排运行时状态（状态页展示 + 降级判定）
rerank_state: dict[str, Any] = {
    "ok": False,        # 最近一次探测/调用是否可用
    "reason": "",       # 不可用原因
    "took_ms": 0.0,     # 最近一次请求耗时
    "probed_at": 0.0,   # 最近一次探测（monotonic）
    "cool_until": 0.0,  # 冷却截止（monotonic）
    "fails": 0,         # 连续失败次数
    "last_ok_at": "",   # 最近一次成功（本地时间）
    "path": "",         # 探测到的可用路径（/rerank 或 /v1/rerank）
}


def _flag(value: str) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def rerank_configured() -> bool:
    """是否配置了重排服务地址。"""
    return bool(RERANK_URL)


def rerank_enabled(conn=None) -> bool:
    """重排是否启用：地址必须已配置；开关 env > 页面设置 > 默认开。"""
    if not RERANK_URL:
        return False
    raw = os.environ.get("L_NOTEPAD_RERANK_ENABLED", "").strip()
    if raw:
        return _flag(raw)
    if conn is not None:
        try:
            configured = get_setting(conn, SETTING_RERANK_ENABLED)
        except Exception:
            configured = ""
        if configured:
            return _flag(configured)
    return True


def rerank_model(conn=None) -> str:
    """重排模型名：env > 页面设置 > 空（由服务端默认模型决定）。"""
    if RERANK_MODEL_ENV:
        return RERANK_MODEL_ENV
    if conn is not None:
        try:
            configured = get_setting(conn, SETTING_RERANK_MODEL)
        except Exception:
            configured = ""
        if configured:
            return configured
    return ""


def _rerank_post(path: str, payload: dict) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{RERANK_URL}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=RERANK_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _rerank_call(query: str, documents: list[str], model_name: str) -> list[tuple[int, float]]:
    """发起重排请求，返回 [(下标, 相关度)]；失败抛异常。"""
    payload: dict[str, Any] = {"query": query, "documents": list(documents)}
    if model_name:
        payload["model"] = model_name
    paths = [str(rerank_state.get("path") or ""), "/rerank", "/v1/rerank"]
    last: Exception | None = None
    for path in dict.fromkeys(p for p in paths if p):
        try:
            data = _rerank_post(path, payload)
        except urllib.error.HTTPError as exc:
            last = exc
            continue  # 路径不存在（404 等）→ 换下一条路径试
        except Exception as exc:  # noqa: BLE001 - 连不上/超时换路径也没用，直接降级
            raise last or exc
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not results:
            last = ValueError("返回结构不符（无 results）")
            continue
        out: list[tuple[int, float]] = []
        for item in results:
            try:
                out.append((int(item["index"]), float(item.get("relevance_score") or 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
        if not out:
            last = ValueError("results 无法解析")
            continue
        with _lock:
            rerank_state["path"] = path
        return out
    raise last or RuntimeError("rerank 请求失败")


def rerank_docs(
    conn, query: str, documents: list[str]
) -> tuple[Optional[list[tuple[int, float]]], dict[str, Any]]:
    """对候选块做重排，返回 ([(下标, 相关度)] | None, 信息)。

    None 表示本轮未使用重排（未配置 / 已关闭 / 冷却中 / 调用失败），信息里带原因与耗时，
    调用方据此退回原融合排序。连续失败 `RERANK_FAIL_LIMIT` 次后进入 `RERANK_COOLDOWN_S` 冷却，
    冷却期内不再发起网络请求。
    """
    info: dict[str, Any] = {"used": False, "model": "", "scored": 0, "took_ms": 0.0, "reason": ""}
    if not documents:
        return None, info
    if not rerank_configured():
        info["reason"] = "未配置 L_NOTEPAD_RERANK_URL"
        return None, info
    if not rerank_enabled(conn):
        info["reason"] = "重排已关闭"
        return None, info
    model_name = rerank_model(conn)
    info["model"] = model_name
    now = time.monotonic()
    with _lock:
        cool_until = float(rerank_state["cool_until"])
        probed_at = float(rerank_state["probed_at"])
        ok = bool(rerank_state["ok"])
        reason = str(rerank_state["reason"] or "")
    if cool_until > now:
        info["reason"] = f"失败冷却中（剩 {int(cool_until - now)}s）"
        return None, info
    if probed_at and now - probed_at < RERANK_PROBE_TTL_S and not ok:
        info["reason"] = reason or "探测不通过"
        return None, info

    started = time.perf_counter()
    try:
        out = _rerank_call(query, documents, model_name)
    except Exception as exc:  # noqa: BLE001 - 降级：本轮退回融合排序
        took = round((time.perf_counter() - started) * 1000, 2)
        with _lock:
            fails = int(rerank_state["fails"]) + 1
            rerank_state.update({
                "ok": False, "reason": f"{type(exc).__name__}: {exc}", "took_ms": took,
                "probed_at": now, "fails": fails,
            })
            if fails >= RERANK_FAIL_LIMIT:
                rerank_state["cool_until"] = now + RERANK_COOLDOWN_S
        info["reason"] = str(rerank_state["reason"])
        info["took_ms"] = took
        return None, info
    took = round((time.perf_counter() - started) * 1000, 2)
    with _lock:
        rerank_state.update({
            "ok": True, "reason": "", "took_ms": took, "probed_at": now,
            "fails": 0, "cool_until": 0.0, "last_ok_at": _now(),
        })
    info.update({"used": True, "scored": len(out), "took_ms": took})
    return out, info


def rerank_status(conn=None) -> dict[str, Any]:
    """重排状态（状态页 / 接口展示）。"""
    with _lock:
        st = dict(rerank_state)
    now = time.monotonic()
    cool_left = max(0.0, float(st["cool_until"]) - now)
    configured = rerank_configured()
    is_on = rerank_enabled(conn)
    reason = str(st["reason"] or "")
    if not configured:
        reason = "未配置 L_NOTEPAD_RERANK_URL"
    elif not is_on:
        reason = "重排已关闭（L_NOTEPAD_RERANK_ENABLED=0 / 页面关闭）"
    elif not reason:
        reason = "尚未调用（首次检索时探测）"
    return {
        "configured": configured,
        "enabled": is_on,
        "available": bool(configured and is_on and st["ok"]),
        "ok": bool(st["ok"]),
        "reason": reason,
        "url": RERANK_URL,
        "model": rerank_model(conn),
        "path": str(st["path"] or ""),
        "top_n": RERANK_TOP_N,
        "timeout_s": RERANK_TIMEOUT_S,
        "took_ms": round(float(st["took_ms"]), 2),
        "fails": int(st["fails"]),
        "cool_s": round(cool_left, 1),
        "cool_after_fails": RERANK_FAIL_LIMIT,
        "last_ok_at": str(st["last_ok_at"] or ""),
        "env_override": {
            "url": RERANK_URL,
            "model": RERANK_MODEL_ENV,
            "enabled": os.environ.get("L_NOTEPAD_RERANK_ENABLED", "").strip(),
            "top_n": os.environ.get("L_NOTEPAD_RERANK_TOP_N", "").strip(),
            "timeout_s": os.environ.get("L_NOTEPAD_RERANK_TIMEOUT_S", "").strip(),
        },
    }


def set_rerank(conn, *, enabled: Optional[bool] = None, model: Optional[str] = None) -> dict[str, Any]:
    """切换重排开关 / 模型（写设置表），并清掉探测与冷却状态以便立即重新探测。"""
    if enabled is not None:
        set_setting(conn, SETTING_RERANK_ENABLED, "1" if enabled else "0")
    if model is not None:
        set_setting(conn, SETTING_RERANK_MODEL, (model or "").strip())
    with _lock:
        rerank_state.update({"ok": False, "reason": "", "probed_at": 0.0, "cool_until": 0.0, "fails": 0})
    return {"ok": True, "status": rerank_status(conn)}


def stats(conn) -> dict[str, Any]:
    """向量索引状态（供状态页展示）。"""
    docs = conn.execute("SELECT COUNT(*) AS c FROM vec_docs").fetchone()["c"]
    chunks = conn.execute("SELECT COUNT(*) AS c FROM vec_chunks").fetchone()["c"]
    dim = conn.execute("SELECT MAX(dim) AS d FROM vec_chunks").fetchone()["d"]
    last = conn.execute("SELECT MAX(embedded_at) AS t FROM vec_docs").fetchone()["t"]
    with _lock:
        cached = len(_cache)
        state = dict(embed_state)
        dl = dict(download_state)
    cur_model = model(conn)
    return {
        "available": available(conn),
        "model": cur_model,
        "url": EMBED_URL,
        "docs": int(docs),
        "chunks": int(chunks),
        "dim": int(dim or 0),
        "last_embedded_at": str(last or ""),
        "chunk_size": chunk_size_for(cur_model),
        "query_instruction": needs_query_instruction(cur_model),
        "batch": BATCH,
        "cached_docs": cached,
        "embed": state,
        "download": dl,
        "catalog": catalog(conn),
    }
