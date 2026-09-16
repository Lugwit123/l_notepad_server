# -*- coding: utf-8 -*-
"""笔记/知识库工作区的向量语义检索（本机 Ollama embedding + SQLite 存向量）

设计（不引入第三方依赖，服务端只有标准库）：
- 分块：按空行/标题切段，超长再硬切，块间留少量重叠；
- 嵌入：调用本机 Ollama HTTP 接口（`/api/embed` 批量，失败回退 `/api/embeddings` 单个）；
- 存储：`vec_chunks` 存块文本 + 归一化后的 float32 向量（BLOB），`vec_docs` 记录
  文件 mtime/size 用于增量重嵌；
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
import threading
import time
import urllib.error
import urllib.request
from array import array
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

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


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按空行/标题切成段，超长段落再按 size 硬切并保留 overlap 重叠。"""
    norm = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not norm:
        return []
    chunks: list[str] = []
    buf = ""
    for para in norm.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(para) <= size:
            buf = para
            continue
        step = max(1, size - overlap)
        for i in range(0, len(para), step):
            piece = para[i : i + size]
            if piece.strip():
                chunks.append(piece)
            if i + size >= len(para):
                break
    if buf:
        chunks.append(buf)
    return chunks[:MAX_CHUNKS_PER_DOC]


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
          chunks INTEGER NOT NULL DEFAULT 0,
          model TEXT NOT NULL DEFAULT '',
          embedded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vec_chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          doc_key TEXT NOT NULL,
          chunk_no INTEGER NOT NULL,
          text TEXT NOT NULL,
          dim INTEGER NOT NULL DEFAULT 0,
          vec BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vec_chunks_doc ON vec_chunks(doc_key);
        """
    )
    init_settings_schema(conn)
    conn.commit()


def _doc_key(source: str, kb_name: str, rel: str) -> str:
    return rel if source == "note" else search_index._kb_key(kb_name, rel)


def _pending_docs(conn, notes_root: Path, force: bool = False) -> list[tuple[str, str, str, Path, str, int, float]]:
    """待嵌入文档：[(doc_key, source, kb_name, 绝对路径, rel, size, mtime)]。

    以 search_docs（词法索引）为准，比对 vec_docs 的 mtime/size 找出新增/变更；
    同时清理 vec_docs 中已不在 search_docs 的条目。
    """
    docs_rows = conn.execute(
        "SELECT note_path AS key, source, kb_name, rel, size, mtime FROM search_docs"
    ).fetchall()
    have = {
        r["doc_key"]: (int(r["size"]), float(r["mtime"]), str(r["model"]))
        for r in conn.execute("SELECT doc_key, size, mtime, model FROM vec_docs")
    }
    mdl = model(conn)
    todo: list[tuple[str, str, str, Path, str, int, float]] = []
    alive: set[str] = set()
    for r in docs_rows:
        key = r["key"]
        alive.add(key)
        prev = have.get(key)
        if not force and prev and prev[0] == int(r["size"]) and abs(prev[1] - float(r["mtime"])) < 1e-6 and prev[2] == mdl:
            continue
        source, kb_name, rel = r["source"], r["kb_name"], r["rel"]
        if source == "note":
            try:
                path = search_index.file_store.resolve_note_path(Path(notes_root), rel)
            except ValueError:
                continue
        else:
            root = conn.execute(
                "SELECT workspace FROM knowledge_bases WHERE name = ?", (kb_name,)
            ).fetchone()
            if not root or not str(root["workspace"]).strip():
                continue
            path = Path(str(root["workspace"])) / rel
        todo.append((key, source, kb_name, path, rel, int(r["size"]), float(r["mtime"])))
    stale = [k for k in have if k not in alive]
    for key in stale:
        drop_doc(conn, key)
    if stale:
        conn.commit()
    return todo


def drop_doc(conn, doc_key: str) -> None:
    conn.execute("DELETE FROM vec_chunks WHERE doc_key = ?", (doc_key,))
    conn.execute("DELETE FROM vec_docs WHERE doc_key = ?", (doc_key,))
    _invalidate()


def embed_doc(conn, doc_key: str, source: str, kb_name: str, rel: str, path: Path,
              size: int, mtime: float) -> int:
    """嵌入一篇文档（先删旧块）。返回块数。"""
    model_name = model(conn)
    text = search_index.file_store.read_text_capped(path, search_index.MAX_INDEX_BYTES)
    block = chunk_size_for(model_name)
    chunks = chunk_text(text, size=block, overlap=max(40, block // 8))
    conn.execute("DELETE FROM vec_chunks WHERE doc_key = ?", (doc_key,))
    if not chunks:
        conn.execute("DELETE FROM vec_docs WHERE doc_key = ?", (doc_key,))
        _invalidate()
        return 0
    model_name = model(conn)
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i : i + BATCH]
        vecs = embed_texts(batch, model_name)
        for j, (chunk, vec) in enumerate(zip(batch, vecs)):
            norm = _normalize(vec)
            conn.execute(
                "INSERT INTO vec_chunks(doc_key, chunk_no, text, dim, vec) VALUES(?,?,?,?,?)",
                (doc_key, i + j, chunk, len(norm), _pack(norm)),
            )
        with _lock:
            embed_state["done"] = int(embed_state.get("done", 0)) + len(batch)
    conn.execute(
        "INSERT INTO vec_docs(doc_key, source, kb_name, rel, size, mtime, chunks, model, embedded_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(doc_key) DO UPDATE SET source=excluded.source, kb_name=excluded.kb_name,"
        " rel=excluded.rel, size=excluded.size, mtime=excluded.mtime, chunks=excluded.chunks,"
        " model=excluded.model, embedded_at=excluded.embedded_at",
        (doc_key, source, kb_name, rel, int(size), float(mtime), len(chunks), model_name, _now()),
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
    for doc_key, source, kb_name, path, rel, size, mtime in todo:
        try:
            embed_doc(conn, doc_key, source, kb_name, rel, path, size, mtime)
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
        for doc_key, source, kb_name, path, rel, size, mtime in todo:
            embed_doc(conn, doc_key, source, kb_name, rel, path, size, mtime)
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


def search(conn, query: str, *, limit: int = 50) -> list[tuple[str, float]]:
    """语义检索：返回 [(doc_key, 相似度)]，按相似度降序（每篇取最高分块）。"""
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
    best: dict[str, float] = {}
    for doc_key, chunks in cache.items():
        top = 0.0
        for _no, vec in chunks:
            score = _dot(qvec, vec)
            if score > top:
                top = score
        if top > 0:
            best[doc_key] = top
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]


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
